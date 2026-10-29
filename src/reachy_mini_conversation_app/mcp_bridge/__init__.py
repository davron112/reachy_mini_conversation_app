"""MCP (Model Context Protocol) bridge: expose remote MCP tools as local Tool subclasses."""

from reachy_mini_conversation_app.mcp_bridge.client import MCPSession, MCPBridgeError
from reachy_mini_conversation_app.mcp_bridge.registry import MCPBridge, build_mcp_bridge


__all__ = ["MCPSession", "MCPBridge", "MCPBridgeError", "build_mcp_bridge"]
