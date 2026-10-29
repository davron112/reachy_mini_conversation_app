"""Adapter that exposes one MCP tool as a Tool subclass."""

from __future__ import annotations
import logging
from typing import Any, Dict

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies
from reachy_mini_conversation_app.mcp_bridge.client import MCPSession, MCPBridgeError


logger = logging.getLogger(__name__)

MCP_TOOL_NAME_PREFIX = "mcp_"
MCP_TOOL_NAME_MAX_LEN = 64


class MCPProxyTool(Tool):
    """Dynamic Tool subclass that forwards calls to an MCP server."""

    def __init__(
        self,
        session: MCPSession,
        remote_name: str,
        description: str,
        input_schema: Dict[str, Any],
    ) -> None:
        """Configure local Tool spec while keeping the remote name for dispatch."""
        self._session = session
        self._remote_name = remote_name
        self.name = self._sanitize_local_name(remote_name)
        self.description = description or f"Remote MCP tool: {remote_name}"
        self.parameters_schema = self._normalize_schema(input_schema)

    @staticmethod
    def _sanitize_local_name(remote: str) -> str:
        cleaned = "".join(c if c.isalnum() or c == "_" else "_" for c in remote.strip())
        candidate = f"{MCP_TOOL_NAME_PREFIX}{cleaned}"
        return candidate[:MCP_TOOL_NAME_MAX_LEN]

    @staticmethod
    def _normalize_schema(schema: Dict[str, Any] | None) -> Dict[str, Any]:
        """Ensure schema is an object with `properties`/`required` for realtime API compat."""
        if not isinstance(schema, dict) or not schema:
            return {"type": "object", "properties": {}, "required": []}
        normalized = dict(schema)
        normalized.setdefault("type", "object")
        normalized.setdefault("properties", {})
        normalized.setdefault("required", [])
        return normalized

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> Dict[str, Any]:
        """Forward the call to the remote MCP server."""
        try:
            payload = await self._session.call_tool(self._remote_name, kwargs)
        except MCPBridgeError as exc:
            logger.warning("MCP tool %s failed: %s", self._remote_name, exc)
            return {"error": str(exc)}
        if payload.get("is_error"):
            return {"error": payload.get("text") or "remote MCP tool returned an error"}
        result: Dict[str, Any] = {"source": "mcp", "tool": self._remote_name}
        if payload.get("text"):
            result["text"] = payload["text"]
        if payload.get("data"):
            result["data"] = payload["data"]
        return result
