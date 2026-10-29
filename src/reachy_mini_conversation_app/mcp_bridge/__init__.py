"""MCP (Model Context Protocol) bridge: expose remote MCP tools as local Tool subclasses."""

from reachy_mini_conversation_app.mcp_bridge.client import MCPSession, MCPBridgeError
from reachy_mini_conversation_app.mcp_bridge.config import MCPServerConfig, load_server_configs
from reachy_mini_conversation_app.mcp_bridge.registry import load_mcp_tools, build_mcp_bridge


__all__ = [
    "MCPBridgeError",
    "MCPServerConfig",
    "MCPSession",
    "build_mcp_bridge",
    "load_mcp_tools",
    "load_server_configs",
]
