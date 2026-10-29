"""Tests for the MCP bridge: adapter behaviour without a real MCP server."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from reachy_mini_conversation_app.mcp_bridge.client import MCPSession, MCPBridgeError
from reachy_mini_conversation_app.mcp_bridge.tool_adapter import (
    MCP_TOOL_NAME_PREFIX,
    MCPProxyTool,
)


def _fake_session() -> MagicMock:
    session = MagicMock(spec=MCPSession)
    session.call_tool = AsyncMock()
    return session


def test_mcpsession_requires_url() -> None:
    """Empty URL must raise immediately, before any I/O."""
    with pytest.raises(MCPBridgeError):
        MCPSession(url="")


def test_proxy_tool_name_sanitisation() -> None:
    """Remote names with dots/slashes become safe local identifiers with the mcp_ prefix."""
    session = _fake_session()
    tool = MCPProxyTool(session=session, remote_name="web.search", description="d", input_schema={})
    assert tool.name.startswith(MCP_TOOL_NAME_PREFIX)
    assert "." not in tool.name
    assert tool.name == "mcp_web_search"


def test_proxy_tool_normalises_empty_schema() -> None:
    """Missing schema must yield a usable empty object schema."""
    session = _fake_session()
    tool = MCPProxyTool(session=session, remote_name="t", description="d", input_schema={})
    assert tool.parameters_schema == {"type": "object", "properties": {}, "required": []}


def test_proxy_tool_preserves_existing_schema_fields() -> None:
    """Properties and required arrays from the MCP schema must survive intact."""
    session = _fake_session()
    schema = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    }
    tool = MCPProxyTool(session=session, remote_name="search", description="d", input_schema=schema)
    assert tool.parameters_schema["properties"]["q"]["type"] == "string"
    assert tool.parameters_schema["required"] == ["q"]


@pytest.mark.asyncio
async def test_proxy_call_forwards_args_and_flattens_result() -> None:
    """Tool call args reach the session unchanged; flattened payload comes back."""
    session = _fake_session()
    session.call_tool.return_value = {
        "is_error": False,
        "text": "result text",
        "data": [{"type": "json", "value": {"x": 1}}],
    }
    tool = MCPProxyTool(session=session, remote_name="ping", description="d", input_schema={})
    result = await tool(deps=MagicMock(), foo="bar")
    session.call_tool.assert_awaited_once_with("ping", {"foo": "bar"})
    assert result["source"] == "mcp"
    assert result["tool"] == "ping"
    assert result["text"] == "result text"
    assert result["data"][0]["value"] == {"x": 1}


@pytest.mark.asyncio
async def test_proxy_call_surfaces_remote_error_field() -> None:
    """When the remote reports is_error=True, our caller sees an error field."""
    session = _fake_session()
    session.call_tool.return_value = {"is_error": True, "text": "boom", "data": None}
    tool = MCPProxyTool(session=session, remote_name="ping", description="d", input_schema={})
    result = await tool(deps=MagicMock())
    assert "error" in result and "boom" in result["error"]


@pytest.mark.asyncio
async def test_proxy_call_wraps_bridge_error() -> None:
    """Transport-level errors are surfaced via the same error key."""
    session = _fake_session()
    session.call_tool.side_effect = MCPBridgeError("network down")
    tool = MCPProxyTool(session=session, remote_name="ping", description="d", input_schema={})
    result = await tool(deps=MagicMock())
    assert "error" in result and "network down" in result["error"]


def test_register_runtime_tools_adds_and_skips_duplicates() -> None:
    """First registration succeeds; the second with the same name is ignored."""
    from reachy_mini_conversation_app.tools.core_tools import (
        ALL_TOOLS,
        ALL_TOOL_SPECS,
        register_runtime_tools,
    )

    session = _fake_session()
    tool_a = MCPProxyTool(session=session, remote_name="alpha_unique_xyz", description="d", input_schema={})
    tool_b = MCPProxyTool(session=session, remote_name="alpha_unique_xyz", description="d", input_schema={})

    before = len(ALL_TOOL_SPECS)
    added = register_runtime_tools([tool_a, tool_b])
    try:
        assert added == [tool_a.name]
        assert tool_a.name in ALL_TOOLS
        assert len(ALL_TOOL_SPECS) == before + 1
    finally:
        ALL_TOOLS.pop(tool_a.name, None)
        ALL_TOOL_SPECS[:] = [s for s in ALL_TOOL_SPECS if s.get("name") != tool_a.name]


def test_register_runtime_tools_ignores_non_tool() -> None:
    """Non-Tool objects in the input list are silently skipped, never crash."""
    from reachy_mini_conversation_app.tools.core_tools import register_runtime_tools

    added = register_runtime_tools(["not a tool", object()])  # type: ignore[list-item]
    assert added == []
