"""Adapter that exposes one remote MCP tool as a local Tool."""

from __future__ import annotations
import re
import logging
from typing import Any, Dict

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies
from reachy_mini_conversation_app.mcp_bridge.client import MCPSession, MCPBridgeError


logger = logging.getLogger(__name__)

TOOL_NAME_MAX_LEN = 64
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_]")
_UNSUPPORTED_SCHEMA_KEYS = ("$schema", "title")


def local_tool_name(server_name: str, remote_name: str) -> str:
    """Build the function name the LLM sees: `<server>_<tool>`, restricted to [A-Za-z0-9_]."""
    raw = f"{server_name.strip()}_{remote_name.strip()}"
    return _UNSAFE_NAME_CHARS.sub("_", raw)[:TOOL_NAME_MAX_LEN]


def normalize_schema(schema: Dict[str, Any] | None) -> Dict[str, Any]:
    """Return an object schema with `properties`/`required`, as realtime backends expect."""
    if not isinstance(schema, dict) or not schema:
        return {"type": "object", "properties": {}, "required": []}
    normalized = {key: value for key, value in schema.items() if key not in _UNSUPPORTED_SCHEMA_KEYS}
    normalized.setdefault("type", "object")
    normalized.setdefault("properties", {})
    normalized.setdefault("required", [])
    return normalized


class MCPProxyTool(Tool):
    """Tool that forwards calls to a remote MCP server."""

    def __init__(
        self,
        session: MCPSession,
        remote_name: str,
        description: str,
        input_schema: Dict[str, Any],
    ) -> None:
        """Expose the remote tool under a server-scoped local name."""
        self._session = session
        self.server_name = session.config.name
        self.remote_name = remote_name
        self.name = local_tool_name(self.server_name, remote_name)
        self.description = description or f"Remote tool {remote_name} from {self.server_name}"
        self.parameters_schema = normalize_schema(input_schema)

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> Dict[str, Any]:
        """Forward the call and translate the outcome into a tool result."""
        try:
            payload = await self._session.call_tool(self.remote_name, kwargs)
        except MCPBridgeError as exc:
            logger.warning("MCP tool %s failed: %s", self.name, exc)
            return {"error": str(exc)}
        if payload.get("is_error"):
            return {"error": payload.get("text") or f"remote tool {self.remote_name} reported an error"}
        result: Dict[str, Any] = {}
        if payload.get("text"):
            result["text"] = payload["text"]
        if payload.get("data"):
            result["data"] = payload["data"]
        return result or {"status": "ok"}
