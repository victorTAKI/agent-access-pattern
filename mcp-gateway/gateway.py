"""MCP Gateway.

Acts as an authenticating/authorizing proxy in front of one or more MCP
servers. Two responsibilities:

1. Verify the incoming bearer token against the Keycloak realm (issuer,
   audience, signature).
2. Enforce a per-agent tool allow-list before forwarding the MCP call
   to the upstream MCP server.

The design is intentionally minimal so it can be extended to N agents /
N MCP servers by editing ``permissions.yaml`` and adding entries to the
``UPSTREAMS`` mapping.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any

import httpx
import jwt
import yaml
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from jwt import PyJWKClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("mcp-gateway")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
OIDC_ISSUER = os.environ["OIDC_ISSUER"].rstrip("/")
OIDC_AUDIENCE = os.getenv("OIDC_AUDIENCE", "mcp-gateway")
UPSTREAM_MCP_URL = os.environ["UPSTREAM_MCP_URL"]
PERMISSIONS_FILE = os.getenv("PERMISSIONS_FILE", "/app/permissions.yaml")
HOST = os.getenv("GATEWAY_HOST", "0.0.0.0")
PORT = int(os.getenv("GATEWAY_PORT", "9000"))

# Headers we forward to the upstream MCP server.
_FORWARDED_HEADERS = {"content-type", "accept", "mcp-session-id", "mcp-protocol-version"}


def _load_permissions() -> dict[str, list[str]]:
    with open(PERMISSIONS_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {k: list(v or []) for k, v in (data.get("agents") or {}).items()}


PERMISSIONS = _load_permissions()
log.info("Loaded permissions: %s", PERMISSIONS)


# ---------------------------------------------------------------------------
# JWT validation
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient:
    jwks_url = f"{OIDC_ISSUER}/protocol/openid-connect/certs"
    return PyJWKClient(jwks_url)


def _verify_token(token: str) -> dict[str, Any]:
    """Verify signature, issuer, expiry, and audience of a Keycloak JWT."""
    try:
        signing_key = _jwks_client().get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            issuer=OIDC_ISSUER,
            options={"verify_aud": False},
        )
    except jwt.PyJWTError as exc:  # noqa: BLE001
        log.warning("JWT verification failed: %s", exc)
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")

    aud = claims.get("aud")
    aud_list = [aud] if isinstance(aud, str) else (aud or [])
    resource_access = claims.get("resource_access") or {}
    if OIDC_AUDIENCE not in aud_list and OIDC_AUDIENCE not in resource_access:
        raise HTTPException(
            status_code=403,
            detail=f"Token is not intended for audience '{OIDC_AUDIENCE}'",
        )
    return claims


def _agent_id(claims: dict[str, Any]) -> str:
    """Best-effort extraction of the *acting* agent identity.

    RFC 8693 style tokens have an ``act`` claim describing the actor.
    Keycloak's standard token exchange v2 also sets ``azp`` (authorized
    party) to the client that requested the exchange, which we use as
    a fallback.
    """
    act = claims.get("act") or {}
    if isinstance(act, dict) and act.get("sub"):
        return str(act["sub"])
    if claims.get("azp"):
        return str(claims["azp"])
    return str(claims.get("client_id") or claims.get("sub") or "unknown")


# ---------------------------------------------------------------------------
# FastAPI proxy (streamable HTTP MCP)
# ---------------------------------------------------------------------------
app = FastAPI(title="MCP Gateway")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _extract_tool_name(body: dict[str, Any]) -> tuple[str | None, Any]:
    """Return ``(tool_name, request_id)`` for MCP tools/call, else ``(None, None)``."""
    if not isinstance(body, dict):
        return None, None
    if body.get("method") == "tools/call":
        params = body.get("params") or {}
        return params.get("name"), body.get("id")
    return None, None


def _authz_denied_response(agent: str, tool: str, request_id: Any) -> JSONResponse:
    """Return a well-formed MCP tool error so the LLM sees it as a normal
    tool result and reacts in natural language instead of throwing."""
    msg = f"Agent '{agent}' does not have permission to call tool '{tool}'."
    log.info("DENY agent=%s tool=%s", agent, tool)
    return JSONResponse(
        status_code=200,
        content={
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": msg}],
            },
        },
    )


def _extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    return authorization.split(" ", 1)[1].strip()


async def _forward(request: Request, method: str, body: bytes | None = None) -> Response:
    fwd_headers = {
        k: v for k, v in request.headers.items() if k.lower() in _FORWARDED_HEADERS
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        upstream = await client.request(
            method, UPSTREAM_MCP_URL, content=body, headers=fwd_headers
        )
    resp_headers = {}
    if "mcp-session-id" in upstream.headers:
        resp_headers["mcp-session-id"] = upstream.headers["mcp-session-id"]
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


@app.post("/mcp")
@app.post("/mcp/")
async def proxy_mcp_post(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    token = _extract_bearer(authorization)
    claims = _verify_token(token)
    agent = _agent_id(claims)

    body = await request.body()
    try:
        parsed = await request.json()
    except Exception:  # noqa: BLE001
        parsed = None

    tool, request_id = _extract_tool_name(parsed) if parsed else (None, None)
    if tool is not None:
        allowed = PERMISSIONS.get(agent, [])
        if tool not in allowed:
            return _authz_denied_response(agent, tool, request_id)
        log.info("ALLOW agent=%s tool=%s", agent, tool)

    return await _forward(request, "POST", body)


@app.get("/mcp")
@app.get("/mcp/")
async def proxy_mcp_get(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    # SSE / server-initiated messages — no tool authz needed, just proxy.
    _verify_token(_extract_bearer(authorization))
    return await _forward(request, "GET")


@app.delete("/mcp")
@app.delete("/mcp/")
async def proxy_mcp_delete(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    # Session teardown.
    _verify_token(_extract_bearer(authorization))
    return await _forward(request, "DELETE")


@app.exception_handler(HTTPException)
async def _http_exc(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "jsonrpc": "2.0",
            "error": {"code": -32000 - exc.status_code, "message": exc.detail},
            "id": None,
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
