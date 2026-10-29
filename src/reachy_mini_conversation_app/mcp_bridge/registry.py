"""Discover tools on configured MCP servers and register the ones the profile selects."""

from __future__ import annotations
import asyncio
import logging
from typing import Sequence
from concurrent.futures import ThreadPoolExecutor

from reachy_mini_conversation_app.tools.core_tools import register_runtime_tools, get_profile_mcp_selectors
from reachy_mini_conversation_app.mcp_bridge.client import MCPSession, MCPBridgeError
from reachy_mini_conversation_app.mcp_bridge.config import MCPServerConfig, load_server_configs
from reachy_mini_conversation_app.mcp_bridge.tool_adapter import MCPProxyTool


logger = logging.getLogger(__name__)

SELECT_ALL = "*"
SELECTOR_SEPARATOR = "/"


def _split(selector: str) -> tuple[str, str]:
    server, _, tool = selector.strip().partition(SELECTOR_SEPARATOR)
    return server.strip(), tool.strip()


def _matches(pattern: str, value: str) -> bool:
    return pattern in ("", SELECT_ALL) or pattern == value


def server_is_selected(server_name: str, selectors: Sequence[str]) -> bool:
    """Return whether any selector can match a tool of this server."""
    return any(_split(selector)[0] in (SELECT_ALL, server_name) for selector in selectors)


def is_selected(server_name: str, remote_name: str, selectors: Sequence[str]) -> bool:
    """Match `*`, `<server>`, `<server>/*`, `*/<tool>` or `<server>/<tool>` selectors from tools.txt."""
    for selector in selectors:
        server, tool = _split(selector)
        if server in (SELECT_ALL, server_name) and _matches(tool, remote_name):
            return True
    return False


async def _discover_server(server: MCPServerConfig, selectors: Sequence[str]) -> list[MCPProxyTool]:
    session = MCPSession(server)
    try:
        specs = await session.list_tools()
    except MCPBridgeError as exc:
        logger.warning("MCP server %r unavailable, its tools are skipped: %s", server.name, exc)
        return []
    selected = [spec for spec in specs if is_selected(server.name, spec["name"], selectors)]
    logger.info("MCP server %r: %d tool(s) offered, %d selected", server.name, len(specs), len(selected))
    return [
        MCPProxyTool(
            session=session,
            remote_name=spec["name"],
            description=spec["description"],
            input_schema=spec["input_schema"],
        )
        for spec in selected
    ]


async def discover_mcp_tools(servers: Sequence[MCPServerConfig], selectors: Sequence[str]) -> list[MCPProxyTool]:
    """Query every relevant server concurrently; unreachable servers contribute nothing."""
    relevant = [server for server in servers if server_is_selected(server.name, selectors)]
    batches = await asyncio.gather(*(_discover_server(server, selectors) for server in relevant))
    return [tool for batch in batches for tool in batch]


async def build_mcp_bridge(
    servers: Sequence[MCPServerConfig] | None = None,
    selectors: Sequence[str] | None = None,
) -> list[str]:
    """Discover selected MCP tools and register them; return the registered tool names."""
    servers = load_server_configs() if servers is None else servers
    selectors = get_profile_mcp_selectors() if selectors is None else selectors
    if not servers:
        logger.info("MCP disabled: no server configured")
        return []
    if not selectors:
        logger.info("MCP servers configured but the profile has no `mcp:` entries in tools.txt")
        return []
    registered = register_runtime_tools(await discover_mcp_tools(servers, selectors))
    logger.info("MCP tools registered: %s", registered)
    return registered


def load_mcp_tools() -> list[str]:
    """Run discovery on a private event loop in a worker thread, leaving the caller's loop untouched."""
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="mcp-discovery") as executor:
        return executor.submit(asyncio.run, build_mcp_bridge()).result()
