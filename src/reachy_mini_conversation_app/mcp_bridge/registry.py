"""Discover tools on env-configured MCP servers and offer them to profiles that select them."""

from __future__ import annotations
import asyncio
import logging
from typing import Sequence
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor

from reachy_mini_conversation_app.mcp_client import McpClientError, RemoteMcpToolClient, RemoteMcpServerConfig
from reachy_mini_conversation_app.tools.core_tools import RemoteMcpTool, register_runtime_tool_provider
from reachy_mini_conversation_app.mcp_bridge.config import MCPServerConfig, load_server_configs


logger = logging.getLogger(__name__)

SELECT_ALL = "*"
SELECTOR_SEPARATOR = "/"


def _split(selector: str) -> tuple[str, str]:
    server, _, tool = selector.strip().partition(SELECTOR_SEPARATOR)
    return server.strip(), tool.strip()


def _matches(pattern: str, value: str) -> bool:
    return pattern in ("", SELECT_ALL) or pattern == value


def is_selected(server_name: str, remote_name: str, selectors: Sequence[str]) -> bool:
    """Match `*`, `<server>`, `<server>/*`, `*/<tool>` or `<server>/<tool>` profile selectors."""
    for selector in selectors:
        server, tool = _split(selector)
        if server in (SELECT_ALL, server_name) and _matches(tool, remote_name):
            return True
    return False


@dataclass(frozen=True)
class DiscoveredTool:
    """One remote tool together with the server and remote name its selectors match on."""

    server: str
    remote_name: str
    tool: RemoteMcpTool


class MCPToolCatalog:
    """Tools discovered at startup; selects the ones a profile opts into."""

    def __init__(self, tools: Sequence[DiscoveredTool]) -> None:
        """Keep the discovered tools for later selection."""
        self._tools = tuple(tools)

    @property
    def tool_names(self) -> list[str]:
        """Return the local names of every discovered tool."""
        return [entry.tool.name for entry in self._tools]

    def __call__(self, selectors: Sequence[str]) -> list[RemoteMcpTool]:
        """Return the discovered tools matched by the profile's selectors."""
        return [entry.tool for entry in self._tools if is_selected(entry.server, entry.remote_name, selectors)]


def _build_client(server: MCPServerConfig) -> RemoteMcpToolClient:
    return RemoteMcpToolClient(
        RemoteMcpServerConfig(
            alias=server.name,
            url=server.url,
            headers=server.request_headers(),
            request_timeout_s=server.discovery_timeout_s,
            tool_timeout_s=server.call_timeout_s,
        )
    )


async def _discover_server(server: MCPServerConfig) -> list[DiscoveredTool]:
    try:
        client = _build_client(server)
        specs = await asyncio.wait_for(client.list_tool_specs(), timeout=server.discovery_timeout_s)
    except (McpClientError, ValueError, TimeoutError) as exc:
        logger.warning("MCP server %r unavailable, its tools are skipped: %s", server.name, exc)
        return []
    logger.info("MCP server %r offers %d tool(s)", server.name, len(specs))
    return [
        DiscoveredTool(
            server=server.name,
            remote_name=spec.remote_name,
            tool=RemoteMcpTool(
                slug=server.name,
                name=spec.namespaced_name,
                description=spec.description,
                parameters_schema=spec.parameters_schema,
                client_tool_name=spec.namespaced_name,
                client=client,
            ),
        )
        for spec in specs
    ]


async def discover_mcp_tools(servers: Sequence[MCPServerConfig]) -> MCPToolCatalog:
    """Query every server concurrently; unreachable servers contribute nothing."""
    batches = await asyncio.gather(*(_discover_server(server) for server in servers))
    return MCPToolCatalog([tool for batch in batches for tool in batch])


async def build_mcp_bridge(servers: Sequence[MCPServerConfig] | None = None) -> MCPToolCatalog | None:
    """Discover tools on the configured servers and offer them to the tool registry."""
    servers = load_server_configs() if servers is None else servers
    if not servers:
        logger.info("MCP bridge disabled: no server configured")
        return None
    catalog = await discover_mcp_tools(servers)
    register_runtime_tool_provider(catalog)
    logger.info("MCP tools discovered: %s", catalog.tool_names)
    return catalog


def load_mcp_tools() -> MCPToolCatalog | None:
    """Run discovery on a private event loop in a worker thread, leaving the caller's loop untouched."""
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="mcp-discovery") as executor:
        return executor.submit(asyncio.run, build_mcp_bridge()).result()
