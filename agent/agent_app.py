"""Agent service.

Exposes a tiny HTTP API used by the UI. For each user request:

  1. Receive the user's Keycloak access token.
  2. Perform an RFC 8693 token exchange with Keycloak, acting as the
     confidential client ``agent-1``. The resulting token still carries
     the user's identity (``sub``) but has ``azp = agent-1`` (and, on
     Keycloak's standard v2 flow, an ``act`` claim identifying the
     agent as the actor).
  3. Call the MCP Gateway with the exchanged token; tool authorization
     is enforced there.
  4. Drive tool use via a Strands agent (with graceful fallback to a
     deterministic router when no LLM provider is configured, so the
     demo can run out of the box).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastmcp import Client as MCPClient
from fastmcp.client.transports import StreamableHttpTransport
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("agent")


OIDC_ISSUER = os.environ["OIDC_ISSUER"].rstrip("/")
AGENT_CLIENT_ID = os.environ["AGENT_CLIENT_ID"]
AGENT_CLIENT_SECRET = os.environ["AGENT_CLIENT_SECRET"]
MCP_GATEWAY_URL = os.environ["MCP_GATEWAY_URL"]
STRANDS_MODEL_PROVIDER = os.getenv("STRANDS_MODEL_PROVIDER", "mock").lower()


# ---------------------------------------------------------------------------
# RFC 8693 Token Exchange
# ---------------------------------------------------------------------------
async def exchange_token(user_token: str) -> str:
    """Exchange the user's access token for one where the agent is the
    authorized party. Uses the OAuth2 Token Exchange grant (RFC 8693).
    """
    url = f"{OIDC_ISSUER}/protocol/openid-connect/token"
    data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "client_id": AGENT_CLIENT_ID,
        "client_secret": AGENT_CLIENT_SECRET,
        "subject_token": user_token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": "mcp-gateway",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(url, data=data)
    if r.status_code != 200:
        log.error("token exchange failed: %s %s", r.status_code, r.text)
        raise HTTPException(status_code=401, detail=f"token exchange failed: {r.text}")
    return r.json()["access_token"]


async def logout_user(refresh_token: str) -> None:
    url = f"{OIDC_ISSUER}/protocol/openid-connect/logout"
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(
            url,
            data={
                "client_id": "ui-client",
                "refresh_token": refresh_token,
            },
        )


# ---------------------------------------------------------------------------
# MCP client wired to the Gateway with the exchanged token
# ---------------------------------------------------------------------------
def _mcp_client(agent_token: str) -> MCPClient:
    transport = StreamableHttpTransport(
        url=MCP_GATEWAY_URL,
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    return MCPClient(transport)


async def _call_tool(agent_token: str, name: str, arguments: dict[str, Any]) -> str:
    async with _mcp_client(agent_token) as client:
        result = await client.call_tool(name, arguments)
    # FastMCP returns a CallToolResult with .content (list of blocks) and .data
    if result.data is not None:
        return _stringify(result.data)
    parts = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts) if parts else "(no content)"


def _stringify(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


# ---------------------------------------------------------------------------
# LLM / router
# ---------------------------------------------------------------------------
async def _list_tools(agent_token: str) -> list[dict[str, Any]]:
    async with _mcp_client(agent_token) as client:
        tools = await client.list_tools()
    return [
        {"name": t.name, "description": t.description or "", "inputSchema": t.inputSchema or {}}
        for t in tools
    ]


async def run_agent(question: str, agent_token: str) -> str:
    """Run the agent. Two modes:

    * ``mock`` (default) — keyword-based routing. Zero external deps,
      always works, perfect for showing the auth pattern.
    * ``bedrock`` / ``openai`` — real Strands agent with tools loaded
      from the MCP gateway.
    """
    if STRANDS_MODEL_PROVIDER == "mock":
        return await _mock_router(question, agent_token)
    return await _strands_router(question, agent_token)


async def _mock_router(question: str, agent_token: str) -> str:
    q = question.lower()
    try:
        if "weather" in q or "météo" in q or "meteo" in q:
            city = "Paris"
            for token in question.split():
                if token[:1].isupper() and token.isalpha() and len(token) > 2:
                    city = token
                    break
            return await _call_tool(agent_token, "get_weather", {"city": city})
        if "employee" in q or "employés" in q or "employes" in q or "staff" in q:
            return await _call_tool(agent_token, "list_employees", {})
    except HTTPException as exc:
        return f"⛔ {exc.detail}"
    return (
        "I can help with two things right now: 'what is the weather?' or "
        "'list the employees of the company'."
    )


async def _strands_router(question: str, agent_token: str) -> str:
    # Lazy import so the mock path has no heavy deps at container start.
    from strands import Agent
    from strands.tools.mcp import MCPClient as StrandsMCPClient
    from mcp.client.streamable_http import streamablehttp_client

    def _transport():
        return streamablehttp_client(
            MCP_GATEWAY_URL,
            headers={"Authorization": f"Bearer {agent_token}"},
        )

    with StrandsMCPClient(_transport) as mcp_client:
        tools = mcp_client.list_tools_sync()
        model = _build_model()
        agent = Agent(
            model=model,
            tools=tools,
            system_prompt=(
                "You are a helpful assistant. Use the available tools to "
                "answer the user. If a tool returns a permission error, "
                "explain that the agent is not authorized for that action."
            ),
        )
        try:
            result = agent(question)
        except Exception as exc:  # noqa: BLE001
            return f"⛔ {exc}"
    return str(result)


def _build_model():
    provider = STRANDS_MODEL_PROVIDER
    if provider == "bedrock":
        from strands.models import BedrockModel

        return BedrockModel(
            model_id=os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-3-5-haiku-20241022-v1:0"),
            region_name=os.getenv("AWS_REGION", "us-east-1"),
        )
    if provider == "openai":
        from strands.models.openai import OpenAIModel

        return OpenAIModel(
            client_args={
                "api_key": os.getenv("OPENAI_API_KEY"),
                "base_url": os.getenv("OPENAI_BASE_URL") or None,
            },
            model_id=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        )
    raise RuntimeError(f"Unsupported STRANDS_MODEL_PROVIDER: {provider}")


# ---------------------------------------------------------------------------
# HTTP API consumed by the UI
# ---------------------------------------------------------------------------
app = FastAPI(title="Agent")


class ChatRequest(BaseModel):
    question: str
    user_token: str


class ChatResponse(BaseModel):
    answer: str
    acting_as: str
    on_behalf_of: str | None = None


class LogoutRequest(BaseModel):
    refresh_token: str


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    agent_token = await exchange_token(req.user_token)
    # Peek at claims for observability (unverified — for display only).
    import base64

    try:
        _, payload_b64, _ = agent_token.split(".")
        payload_b64 += "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:  # noqa: BLE001
        claims = {}
    user = claims.get("sub")
    answer = await run_agent(req.question, agent_token)
    return ChatResponse(answer=answer, acting_as=AGENT_CLIENT_ID, on_behalf_of=user)


@app.post("/logout")
async def logout(req: LogoutRequest) -> dict[str, str]:
    await logout_user(req.refresh_token)
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("AGENT_HOST", "0.0.0.0"), port=int(os.getenv("AGENT_PORT", "8000")))
