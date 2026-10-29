"""MCP server configuration loaded from environment variables."""

from __future__ import annotations
import os
import json
import logging
from typing import Any, Mapping
from dataclasses import field, dataclass


logger = logging.getLogger(__name__)

ENV_SERVERS = "MCP_SERVERS"
ENV_URL = "MCP_SERVER_URL"
ENV_NAME = "MCP_SERVER_NAME"
ENV_API_KEY = "MCP_API_KEY"
ENV_EXTRA_HEADERS = "MCP_EXTRA_HEADERS"
ENV_DISCOVERY_TIMEOUT = "MCP_DISCOVERY_TIMEOUT_S"
ENV_CALL_TIMEOUT = "MCP_CALL_TIMEOUT_S"

DEFAULT_SERVER_NAME = "mcp"
DEFAULT_DISCOVERY_TIMEOUT_S = 5.0
DEFAULT_CALL_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class MCPServerConfig:
    """Connection settings for one remote MCP server."""

    name: str
    url: str
    api_key: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    discovery_timeout_s: float = DEFAULT_DISCOVERY_TIMEOUT_S
    call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S

    def request_headers(self) -> dict[str, str]:
        """Return HTTP headers including bearer auth when an API key is set."""
        headers = dict(self.headers)
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers


def _positive_float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %.1f", key, raw, default)
        return default
    return value if value > 0 else default


def _string_map(raw: Any, source: str) -> dict[str, str]:
    if raw in (None, ""):
        return {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("%s is not valid JSON; ignoring", source)
            return {}
    if not isinstance(raw, dict):
        logger.warning("%s must be a JSON object; ignoring", source)
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _env_secret(env: Mapping[str, str], key: str) -> str | None:
    if not key:
        return None
    return (env.get(key) or "").strip() or None


def _servers_from_json(env: Mapping[str, str], discovery_s: float, call_s: float) -> list[MCPServerConfig]:
    raw = (env.get(ENV_SERVERS) or "").strip()
    if not raw:
        return []
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("%s is not valid JSON; ignoring", ENV_SERVERS)
        return []
    if not isinstance(entries, list):
        logger.warning("%s must be a JSON list; ignoring", ENV_SERVERS)
        return []

    servers: list[MCPServerConfig] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            logger.warning("%s[%d] must be an object; skipping", ENV_SERVERS, index)
            continue
        name = str(entry.get("name") or "").strip()
        url = str(entry.get("url") or "").strip()
        if not name or not url:
            logger.warning("%s[%d] needs both 'name' and 'url'; skipping", ENV_SERVERS, index)
            continue
        servers.append(
            MCPServerConfig(
                name=name,
                url=url,
                api_key=_env_secret(env, str(entry.get("api_key_env") or "").strip()),
                headers=_string_map(entry.get("headers"), f"{ENV_SERVERS}[{index}].headers"),
                discovery_timeout_s=discovery_s,
                call_timeout_s=call_s,
            )
        )
    return servers


def _legacy_server(env: Mapping[str, str], discovery_s: float, call_s: float) -> MCPServerConfig | None:
    url = (env.get(ENV_URL) or "").strip()
    if not url:
        return None
    return MCPServerConfig(
        name=(env.get(ENV_NAME) or "").strip() or DEFAULT_SERVER_NAME,
        url=url,
        api_key=_env_secret(env, ENV_API_KEY),
        headers=_string_map(env.get(ENV_EXTRA_HEADERS), ENV_EXTRA_HEADERS),
        discovery_timeout_s=discovery_s,
        call_timeout_s=call_s,
    )


def load_server_configs(env: Mapping[str, str] | None = None) -> list[MCPServerConfig]:
    """Collect MCP servers from MCP_SERVERS and the single-server MCP_SERVER_URL variables."""
    source = os.environ if env is None else env
    discovery_s = _positive_float(source, ENV_DISCOVERY_TIMEOUT, DEFAULT_DISCOVERY_TIMEOUT_S)
    call_s = _positive_float(source, ENV_CALL_TIMEOUT, DEFAULT_CALL_TIMEOUT_S)

    servers = _servers_from_json(source, discovery_s, call_s)
    legacy = _legacy_server(source, discovery_s, call_s)
    if legacy is not None:
        servers.append(legacy)

    unique: dict[str, MCPServerConfig] = {}
    for server in servers:
        if server.name in unique:
            logger.warning("Duplicate MCP server name %r; keeping the first definition", server.name)
            continue
        unique[server.name] = server
    return list(unique.values())
