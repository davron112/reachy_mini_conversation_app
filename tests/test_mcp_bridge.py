"""Tests for the MCP bridge: env config, selectors, discovery and registry integration."""

import json
import socket
import asyncio
from typing import Any

import pytest

from reachy_mini_conversation_app.mcp_bridge import registry
from reachy_mini_conversation_app.mcp_client import RemoteToolSpec, McpTransportError, RemoteMcpToolClient
from reachy_mini_conversation_app.mcp_bridge.config import (
    DEFAULT_SERVER_NAME,
    DEFAULT_CALL_TIMEOUT_S,
    DEFAULT_DISCOVERY_TIMEOUT_S,
    MCPServerConfig,
    load_server_configs,
)


def _server(name: str = "srv", url: str = "http://127.0.0.1:1/mcp", **kwargs: Any) -> MCPServerConfig:
    return MCPServerConfig(name=name, url=url, **kwargs)


def _spec(server: str, remote_name: str) -> RemoteToolSpec:
    return RemoteToolSpec(
        server_alias=server,
        remote_name=remote_name,
        namespaced_name=f"{server}__{remote_name}",
        description=remote_name,
        parameters_schema={"type": "object", "properties": {}},
    )


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
    """The `mcp:` selector grammar matches servers and tools as documented."""
    assert registry.is_selected(server, tool, selectors) is expected


@pytest.fixture
def registered_providers(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Capture provider registration so tests never touch the global tool registry."""
    captured: list[Any] = []
    monkeypatch.setattr(registry, "register_runtime_tool_provider", captured.append)
    return captured


@pytest.fixture
def fake_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve two tools per reachable server and refuse the server named `down`."""

    async def list_tool_specs(self: RemoteMcpToolClient) -> list[RemoteToolSpec]:
        alias = self.server.alias
        if alias == "down":
            raise McpTransportError("connection refused")
        return [_spec(alias, "add"), _spec(alias, "secret")]

    monkeypatch.setattr(RemoteMcpToolClient, "list_tool_specs", list_tool_specs)


@pytest.mark.asyncio
async def test_build_bridge_without_servers_registers_nothing(registered_providers: list[Any]) -> None:
    """No configured server means no provider and no network traffic."""
    assert await registry.build_mcp_bridge(servers=[]) is None
    assert registered_providers == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_discovery")
async def test_build_bridge_tolerates_failing_server_and_selects_per_profile(registered_providers: list[Any]) -> None:
    """Reachable servers contribute their tools; selectors decide which ones a profile gets."""
    catalog = await registry.build_mcp_bridge(servers=[_server("calc"), _server("down"), _server("other")])

    assert registered_providers == [catalog]
    assert catalog is not None
    assert catalog.tool_names == ["calc__add", "calc__secret", "other__add", "other__secret"]
    assert [tool.name for tool in catalog(["calc/add", "down"])] == ["calc__add"]
    assert [tool.name for tool in catalog(["*/add"])] == ["calc__add", "other__add"]
    assert catalog([]) == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("fake_discovery")
async def test_discovered_tool_calls_its_own_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """A selected tool forwards the call to the client of the server it was discovered on."""
    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def call_tool(self: RemoteMcpToolClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((self.server.alias, name, arguments))
        return {"status": "ok", "namespaced_tool_name": name, "text": "3"}

    monkeypatch.setattr(RemoteMcpToolClient, "call_tool", call_tool)
    catalog = await registry.discover_mcp_tools([_server("calc", api_key="k")])
    (tool,) = catalog(["calc/add"])

    result = await tool(deps=None, a=1, b=2)  # type: ignore[arg-type]

    assert calls == [("calc", "calc__add", {"a": 1, "b": 2})]
    assert result["text"] == "3"
    assert tool._client.server.headers["Authorization"] == "Bearer k"


@pytest.mark.asyncio
async def test_invalid_server_config_is_skipped() -> None:
    """Remote plain-HTTP servers and unusable names are refused by the shared MCP client and skipped."""
    catalog = await registry.discover_mcp_tools(
        [_server("lan", url="http://192.0.2.1/mcp"), _server("bad-name", url="https://example.com/mcp")]
    )
    assert catalog.tool_names == []


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.mark.asyncio
async def test_unreachable_server_contributes_nothing() -> None:
    """A refused connection is logged and skipped instead of breaking startup."""
    catalog = await registry.discover_mcp_tools(
        [_server(url=f"http://127.0.0.1:{_free_port()}/mcp", discovery_timeout_s=2)]
    )
    assert catalog.tool_names == []


@pytest.mark.asyncio
async def test_silent_server_is_bounded_by_discovery_timeout() -> None:
    """A server that accepts but never answers cannot stall startup beyond the discovery timeout."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        loop = asyncio.get_running_loop()
        started = loop.time()
        catalog = await registry.discover_mcp_tools(
            [_server(url=f"http://127.0.0.1:{port}/mcp", discovery_timeout_s=0.3)]
        )
        assert catalog.tool_names == []
        assert loop.time() - started < 5


@pytest.mark.asyncio
async def test_load_mcp_tools_runs_off_the_callers_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovery runs on its own loop in a worker thread, so a running caller loop is untouched."""
    caller_loop = asyncio.get_running_loop()
    discovery_loops: list[asyncio.AbstractEventLoop] = []
    sentinel = registry.MCPToolCatalog([])

    async def fake_build() -> registry.MCPToolCatalog:
        discovery_loops.append(asyncio.get_running_loop())
        return sentinel

    monkeypatch.setattr(registry, "build_mcp_bridge", fake_build)

    assert registry.load_mcp_tools() is sentinel
    assert discovery_loops and discovery_loops[0] is not caller_loop
    assert asyncio.get_running_loop() is caller_loop
