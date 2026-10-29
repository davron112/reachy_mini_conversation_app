"""Bootstrap an MCP session + register every discovered tool into ALL_TOOLS."""

from __future__ import annotations
import os
import asyncio
import logging
from dataclasses import field, dataclass

from reachy_mini_conversation_app.tools.core_tools import register_runtime_tools
from reachy_mini_conversation_app.mcp_bridge.client import MCPSession, MCPBridgeError
from reachy_mini_conversation_app.mcp_bridge.tool_adapter import MCPProxyTool


logger = logging.getLogger(__name__)

ENV_URL = "MCP_SERVER_URL"
ENV_API_KEY = "MCP_API_KEY"
ENV_EXTRA_HEADERS = "MCP_EXTRA_HEADERS"  # JSON object string
ENV_DISCOVERY_TIMEOUT = "MCP_DISCOVERY_TIMEOUT_S"

DEFAULT_DISCOVERY_TIMEOUT_S = 15.0


@dataclass
class MCPBridge:
    """Live MCP bridge: open session + the tools we registered locally."""

    session: MCPSession
    tool_names: list[str] = field(default_factory=list)

    async def close(self) -> None:
        """Tear down the underlying session."""
        await self.session.close()


def _parse_extra_headers(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    import json

    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("%s is not valid JSON; ignoring", ENV_EXTRA_HEADERS)
        return {}
    if not isinstance(decoded, dict):
        logger.warning("%s must be a JSON object; ignoring", ENV_EXTRA_HEADERS)
        return {}
    return {str(k): str(v) for k, v in decoded.items()}


async def build_mcp_bridge() -> MCPBridge | None:
    """Connect, discover tools, register them, return the live bridge.

    Returns None when MCP is disabled (no URL) or discovery fails — callers should
    treat that as "MCP unavailable" rather than fatal.
    """
    url = (os.getenv(ENV_URL) or "").strip()
    if not url:
        logger.info("MCP disabled: %s not set", ENV_URL)
        return None

    api_key = (os.getenv(ENV_API_KEY) or "").strip() or None
    extra_headers = _parse_extra_headers(os.getenv(ENV_EXTRA_HEADERS))
    try:
        timeout_s = float(os.getenv(ENV_DISCOVERY_TIMEOUT, DEFAULT_DISCOVERY_TIMEOUT_S))
    except ValueError:
        timeout_s = DEFAULT_DISCOVERY_TIMEOUT_S

    session = MCPSession(url=url, api_key=api_key, extra_headers=extra_headers)
    try:
        tools = await asyncio.wait_for(session.list_tools(), timeout=timeout_s)
    except asyncio.TimeoutError:
        logger.warning("MCP discovery timed out after %.1fs (url=%s)", timeout_s, url)
        await session.close()
        return None
    except MCPBridgeError as exc:
        logger.warning("MCP discovery failed: %s", exc)
        await session.close()
        return None

    if not tools:
        logger.warning("MCP server returned 0 tools")
        await session.close()
        return None

    proxies = [
        MCPProxyTool(
            session=session,
            remote_name=spec["name"],
            description=spec["description"],
            input_schema=spec.get("inputSchema") or {},
        )
        for spec in tools
    ]
    registered = register_runtime_tools(proxies)
    logger.info(
        "MCP bridge ready: %d tool(s) registered from %s -> %s",
        len(registered),
        url,
        registered,
    )
    return MCPBridge(session=session, tool_names=registered)
