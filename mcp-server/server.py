"""MCP Server exposing 2 demo tools over streamable HTTP.

The server itself is intentionally *not* auth-aware. Enforcement of
authentication + fine-grained authorization lives in the MCP Gateway
that fronts it. This matches the real-world pattern of putting policy
at the edge, keeping tool servers focused on business logic.
"""
from __future__ import annotations

import os

from fastmcp import FastMCP

mcp = FastMCP("mcp-server-1")


@mcp.tool
def get_weather(city: str = "Paris") -> str:
    """Return the current weather for the given city.

    This is a stub that always returns "sunny" for demo purposes.
    """
    return f"The weather in {city} is sunny."


@mcp.tool
def list_employees() -> list[dict[str, str]]:
    """List employees of the company.

    Sensitive endpoint used to demonstrate authorization: agent-1 should
    NOT be allowed to call this through the gateway.
    """
    return [
        {"name": "Ada Lovelace", "role": "Engineer"},
        {"name": "Alan Turing", "role": "Cryptographer"},
        {"name": "Grace Hopper", "role": "Compiler Lead"},
        {"name": "Katherine Johnson", "role": "Mathematician"},
        {"name": "Margaret Hamilton", "role": "Software Engineer"},
    ]


if __name__ == "__main__":
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "9001"))
    mcp.run(transport="http", host=host, port=port, path="/mcp")
