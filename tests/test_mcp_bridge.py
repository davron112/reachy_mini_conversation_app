"""Tests for the MCP bridge: config, selectors, adapter, registration and transport failures."""

import json
import socket
import asyncio
import importlib
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from reachy_mini_conversation_app.mcp_bridge import registry
from reachy_mini_conversation_app.mcp_bridge.client import MCPSession, MCPBridgeError, _flatten_result
from reachy_mini_conversation_app.mcp_bridge.config import (
    DEFAULT_SERVER_NAME,
    DEFAULT_CALL_TIMEOUT_S,
    DEFAULT_DISCOVERY_TIMEOUT_S,
    MCPServerConfig,
    load_server_configs,
)
from reachy_mini_conversation_app.mcp_bridge.tool_adapter import MCPProxyTool, local_tool_name, normalize_schema


def _server(name: str = "srv", url: str = "http://127.0.0.1:1/mcp", **kwargs: Any) -> MCPServerConfig:
    return MCPServerConfig(name=name, url=url, **kwargs)


def _fake_session(name: str = "srv") -> MagicMock:
    session = MagicMock(spec=MCPSession)
    session.config = _server(name)
    session.call_tool = AsyncMock()
    return session


def _core_tools() -> Any:
    return importlib.import_module("reachy_mini_conversation_app.tools.core_tools")


def test_mcpsession_requires_url() -> None:
    """Empty URL must raise immediately, before any I/O."""
    with pytest.raises(MCPBridgeError):
        MCPSession(_server(url=""))


def test_config_empty_env_has_no_servers() -> None:
    """Without MCP variables the bridge is disabled."""
    assert load_server_configs({}) == []


def test_config_legacy_single_server() -> None:
    """MCP_SERVER_URL keeps working, with default name, bearer key and extra headers."""
    servers = load_server_configs(
        {
            "MCP_SERVER_URL": "http://host/mcp",
            "MCP_API_KEY": "secret",
            "MCP_EXTRA_HEADERS": '{"X-Tenant": "reachy"}',
        }
    )
    assert len(servers) == 1
    server = servers[0]
    assert server.name == DEFAULT_SERVER_NAME
    assert server.request_headers() == {"X-Tenant": "reachy", "Authorization": "Bearer secret"}
    assert server.discovery_timeout_s == DEFAULT_DISCOVERY_TIMEOUT_S
    assert server.call_timeout_s == DEFAULT_CALL_TIMEOUT_S


def test_config_multiple_servers_with_key_env_and_timeouts() -> None:
    """MCP_SERVERS entries resolve api_key_env indirectly and share the configured timeouts."""
    env = {
        "MCP_SERVERS": json.dumps(
            [
                {"name": "home", "url": "http://home/mcp", "api_key_env": "HOME_KEY", "headers": {"X-A": "1"}},
                {"name": "web", "url": "http://web/mcp"},
            ]
        ),
        "HOME_KEY": "k1",
        "MCP_SERVER_URL": "http://legacy/mcp",
        "MCP_SERVER_NAME": "legacy",
        "MCP_DISCOVERY_TIMEOUT_S": "2.5",
        "MCP_CALL_TIMEOUT_S": "7",
    }
    servers = {server.name: server for server in load_server_configs(env)}
    assert set(servers) == {"home", "web", "legacy"}
    assert servers["home"].request_headers() == {"X-A": "1", "Authorization": "Bearer k1"}
    assert servers["web"].api_key is None
    assert servers["legacy"].discovery_timeout_s == 2.5
    assert servers["legacy"].call_timeout_s == 7.0


@pytest.mark.parametrize(
    "raw",
    ["not json", '{"name": "x"}', '[{"name": "no_url"}, 5]'],
)
def test_config_invalid_servers_json_is_ignored(raw: str) -> None:
    """Malformed MCP_SERVERS never raises; bad entries are skipped."""
    assert load_server_configs({"MCP_SERVERS": raw}) == []


def test_config_duplicate_names_keep_first_and_bad_timeouts_fall_back() -> None:
    """Duplicate names keep the first entry; non-numeric or non-positive timeouts use defaults."""
    env = {
        "MCP_SERVERS": json.dumps([{"name": "mcp", "url": "http://first/mcp"}]),
        "MCP_SERVER_URL": "http://second/mcp",
        "MCP_DISCOVERY_TIMEOUT_S": "abc",
        "MCP_CALL_TIMEOUT_S": "-1",
    }
    servers = load_server_configs(env)
    assert [server.url for server in servers] == ["http://first/mcp"]
    assert servers[0].discovery_timeout_s == DEFAULT_DISCOVERY_TIMEOUT_S
    assert servers[0].call_timeout_s == DEFAULT_CALL_TIMEOUT_S


def test_local_tool_name_is_server_scoped_and_sanitised() -> None:
    """Remote names become `<server>_<tool>` limited to [A-Za-z0-9_] and 64 chars."""
    assert local_tool_name("mcp", "web.search") == "mcp_web_search"
    assert local_tool_name("my-home", "lights/on") == "my_home_lights_on"
    assert len(local_tool_name("s", "x" * 200)) == 64


def test_normalize_schema_fills_defaults_and_strips_meta_keys() -> None:
    """Empty schemas become empty objects; `$schema`/`title` are dropped, properties survive."""
    assert normalize_schema({}) == {"type": "object", "properties": {}, "required": []}
    schema = {
        "$schema": "x",
        "title": "addArguments",
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    }
    assert normalize_schema(schema) == {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    }


def test_proxy_tool_exposes_spec() -> None:
    """The proxy spec carries the scoped name and the remote description."""
    tool = MCPProxyTool(_fake_session("home"), remote_name="lights", description="Toggle", input_schema={})
    spec = tool.spec()
    assert spec["name"] == "home_lights"
    assert spec["description"] == "Toggle"
    assert spec["parameters"]["type"] == "object"


@pytest.mark.asyncio
async def test_proxy_call_forwards_args_and_flattens_result() -> None:
    """Tool call args reach the session unchanged; text and data come back."""
    session = _fake_session()
    session.call_tool.return_value = {"is_error": False, "text": "result text", "data": [{"type": "image"}]}
    tool = MCPProxyTool(session, remote_name="ping", description="d", input_schema={})
    result = await tool(deps=MagicMock(), foo="bar")
    session.call_tool.assert_awaited_once_with("ping", {"foo": "bar"})
    assert result == {"text": "result text", "data": [{"type": "image"}]}


@pytest.mark.asyncio
async def test_proxy_call_empty_result_reports_ok() -> None:
    """A successful call with no content still yields a non-empty result."""
    session = _fake_session()
    session.call_tool.return_value = {"is_error": False, "text": None, "data": None}
    tool = MCPProxyTool(session, remote_name="ping", description="d", input_schema={})
    assert await tool(deps=MagicMock()) == {"status": "ok"}


@pytest.mark.asyncio
async def test_proxy_call_surfaces_remote_error_field() -> None:
    """When the remote reports is_error, the caller sees an error field."""
    session = _fake_session()
    session.call_tool.return_value = {"is_error": True, "text": "boom", "data": None}
    tool = MCPProxyTool(session, remote_name="ping", description="d", input_schema={})
    assert (await tool(deps=MagicMock())) == {"error": "boom"}


@pytest.mark.asyncio
async def test_proxy_call_wraps_bridge_error() -> None:
    """Transport-level errors are surfaced via the error key."""
    session = _fake_session()
    session.call_tool.side_effect = MCPBridgeError("network down")
    tool = MCPProxyTool(session, remote_name="ping", description="d", input_schema={})
    assert (await tool(deps=MagicMock())) == {"error": "network down"}


def test_flatten_result_prefers_text_and_keeps_other_content() -> None:
    """Text blocks are joined; non-text blocks are kept; structured output only fills in for missing text."""
    image = MagicMock(type="image")
    image.model_dump.return_value = {"type": "image", "mimeType": "image/png"}
    with_text = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="a"), SimpleNamespace(type="text", text="b"), image],
        structured_content={"result": "ab"},
        is_error=False,
    )
    assert _flatten_result(with_text) == {
        "is_error": False,
        "text": "a\nb",
        "data": [{"type": "image", "value": {"type": "image", "mimeType": "image/png"}}],
    }
    structured_only = SimpleNamespace(content=[], structured_content={"x": 1}, is_error=True)
    assert _flatten_result(structured_only) == {
        "is_error": True,
        "text": None,
        "data": [{"type": "structured", "value": {"x": 1}}],
    }


@pytest.mark.parametrize(
    ("selectors", "server", "tool", "expected"),
    [
        (["*"], "a", "t", True),
        (["a"], "a", "t", True),
        (["a/*"], "a", "t", True),
        (["a/t"], "a", "t", True),
        (["a/u"], "a", "t", False),
        (["b"], "a", "t", False),
        (["*/t"], "b", "t", True),
        ([], "a", "t", False),
    ],
)
def test_is_selected(selectors: list[str], server: str, tool: str, expected: bool) -> None:
    """Selector grammar from tools.txt matches servers and tools as documented."""
    assert registry.is_selected(server, tool, selectors) is expected


@pytest.fixture
def captured_registration(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Any]]:
    """Replace the global registry with a capture list so tests stay isolated."""
    captured: list[Any] = []

    def fake_register(tools: list[Any]) -> list[str]:
        captured.extend(tools)
        return [tool.name for tool in tools]

    monkeypatch.setattr(registry, "register_runtime_tools", fake_register)
    yield captured


@pytest.mark.asyncio
async def test_build_bridge_skips_discovery_without_selectors(
    monkeypatch: pytest.MonkeyPatch, captured_registration: list[Any]
) -> None:
    """A profile without `mcp:` entries never contacts any server."""
    list_tools = AsyncMock()
    monkeypatch.setattr(MCPSession, "list_tools", list_tools)
    assert await registry.build_mcp_bridge(servers=[_server()], selectors=[]) == []
    list_tools.assert_not_awaited()


@pytest.mark.asyncio
async def test_build_bridge_without_servers_returns_empty(captured_registration: list[Any]) -> None:
    """No configured server means nothing is registered."""
    assert await registry.build_mcp_bridge(servers=[], selectors=["*"]) == []


@pytest.mark.asyncio
async def test_build_bridge_filters_tools_and_tolerates_failing_server(
    monkeypatch: pytest.MonkeyPatch, captured_registration: list[Any]
) -> None:
    """Only selected tools of reachable servers are registered; a failing server is skipped."""

    async def fake_list_tools(self: MCPSession) -> list[dict[str, Any]]:
        if self.config.name == "down":
            raise MCPBridgeError("connection refused")
        return [
            {"name": "add", "description": "Add", "input_schema": {}},
            {"name": "secret", "description": "Hidden", "input_schema": {}},
        ]

    monkeypatch.setattr(MCPSession, "list_tools", fake_list_tools)
    names = await registry.build_mcp_bridge(
        servers=[_server("calc"), _server("down"), _server("other")],
        selectors=["calc/add", "down"],
    )
    assert names == ["calc_add"]
    assert captured_registration[0].remote_name == "add"


@pytest.mark.asyncio
async def test_load_mcp_tools_runs_off_the_callers_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovery runs on its own loop in a worker thread, so a running caller loop is untouched."""
    caller_loop = asyncio.get_running_loop()
    discovery_loops: list[asyncio.AbstractEventLoop] = []

    async def fake_build() -> list[str]:
        discovery_loops.append(asyncio.get_running_loop())
        return ["x_y"]

    monkeypatch.setattr(registry, "build_mcp_bridge", fake_build)

    assert registry.load_mcp_tools() == ["x_y"]
    assert discovery_loops and discovery_loops[0] is not caller_loop
    assert asyncio.get_running_loop() is caller_loop


def test_register_runtime_tools_never_overrides_builtins() -> None:
    """An MCP tool whose scoped name equals a built-in (`task` + `status`) is rejected."""
    core_tools = _core_tools()
    builtin = core_tools.ALL_TOOLS["task_status"]
    before = list(core_tools.ALL_TOOL_SPECS)
    clash = MCPProxyTool(_fake_session("task"), remote_name="status", description="d", input_schema={})
    assert clash.name == "task_status"
    assert core_tools.register_runtime_tools([clash]) == []
    assert core_tools.ALL_TOOLS["task_status"] is builtin
    assert core_tools.ALL_TOOL_SPECS == before


def test_register_runtime_tools_adds_and_skips_duplicates() -> None:
    """First registration succeeds; a second tool with the same name is ignored."""
    core_tools = _core_tools()
    tool_a = MCPProxyTool(_fake_session(), remote_name="alpha_unique_xyz", description="d", input_schema={})
    tool_b = MCPProxyTool(_fake_session(), remote_name="alpha_unique_xyz", description="d", input_schema={})

    before = len(core_tools.ALL_TOOL_SPECS)
    added = core_tools.register_runtime_tools([tool_a, tool_b])
    try:
        assert added == [tool_a.name]
        assert core_tools.ALL_TOOLS[tool_a.name] is tool_a
        assert len(core_tools.ALL_TOOL_SPECS) == before + 1
    finally:
        core_tools.ALL_TOOLS.pop(tool_a.name, None)
        core_tools.ALL_TOOL_SPECS[:] = [s for s in core_tools.ALL_TOOL_SPECS if s.get("name") != tool_a.name]


def test_register_runtime_tools_ignores_non_tool() -> None:
    """Non-Tool objects in the input list are skipped, never crash."""
    assert _core_tools().register_runtime_tools(["not a tool", object()]) == []


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.mark.asyncio
async def test_session_unreachable_server_raises_bridge_error() -> None:
    """A refused connection becomes MCPBridgeError instead of leaking transport exceptions."""
    session = MCPSession(_server(url=f"http://127.0.0.1:{_free_port()}/mcp", discovery_timeout_s=2))
    with pytest.raises(MCPBridgeError, match="failed"):
        await session.list_tools()


@pytest.mark.asyncio
async def test_session_silent_server_times_out() -> None:
    """A server that accepts but never answers is bounded by the configured call timeout."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        session = MCPSession(_server(url=f"http://127.0.0.1:{port}/mcp", call_timeout_s=0.3))
        with pytest.raises(MCPBridgeError, match="timed out"):
            await session.call_tool("anything", {})
