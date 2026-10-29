"""Async MCP client over the streamable HTTP transport.

Every RPC opens its own short-lived session inside the caller's event loop and closes it
before returning. No transport state outlives a call, so discovery can run on a startup
loop while tool calls run on the realtime handler's loop, and shutdown has nothing to
release.
"""

from __future__ import annotations
import asyncio
import logging
from typing import Any, TypeVar, Callable, Awaitable

from reachy_mini_conversation_app.mcp_bridge.config import MCPServerConfig


logger = logging.getLogger(__name__)

T = TypeVar("T")


class MCPBridgeError(RuntimeError):
    """Raised when the MCP bridge cannot satisfy a request."""


def _load_sdk() -> tuple[Any, Any, Any]:
    try:
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as exc:
        raise MCPBridgeError("mcp package not installed; install the 'mcp' extra") from exc
    return Client, streamable_http_client, httpx2


class MCPSession:
    """Stateless MCP client bound to one server configuration."""

    def __init__(self, config: MCPServerConfig) -> None:
        """Validate and store the server configuration; no I/O happens here."""
        if not config.url:
            raise MCPBridgeError("MCP server URL is required")
        self._config = config

    @property
    def config(self) -> MCPServerConfig:
        """Return the server configuration."""
        return self._config

    async def _run(self, operation: Callable[[Any], Awaitable[T]], timeout_s: float, label: str) -> T:
        client_cls, transport_factory, httpx2 = _load_sdk()

        async def execute() -> T:
            http_client = httpx2.AsyncClient(
                headers=self._config.request_headers(),
                timeout=httpx2.Timeout(timeout_s),
                follow_redirects=True,
            )
            async with http_client:
                transport = transport_factory(self._config.url, http_client=http_client)
                async with client_cls(transport) as client:
                    return await operation(client)

        try:
            return await asyncio.wait_for(execute(), timeout=timeout_s)
        except Exception as exc:  # noqa: BLE001
            if _is_timeout(exc, httpx2.TimeoutException):
                raise MCPBridgeError(f"{label} timed out after {timeout_s:.1f}s ({self._config.name})") from exc
            raise MCPBridgeError(f"{label} failed ({self._config.name}): {_describe(exc)}") from exc

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return every tool the server exposes as name/description/input_schema dicts."""

        async def operation(client: Any) -> list[dict[str, Any]]:
            tools: list[Any] = []
            cursor: str | None = None
            while True:
                page = await client.list_tools(cursor=cursor)
                tools.extend(page.tools)
                cursor = page.next_cursor
                if not cursor:
                    return [
                        {
                            "name": tool.name,
                            "description": (tool.description or "").strip(),
                            "input_schema": tool.input_schema or {},
                        }
                        for tool in tools
                    ]

        return await self._run(operation, self._config.discovery_timeout_s, "MCP tool discovery")

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call one remote tool and flatten its content into text plus structured data."""

        async def operation(client: Any) -> dict[str, Any]:
            result = await client.call_tool(name, arguments)
            return _flatten_result(result)

        return await self._run(operation, self._config.call_timeout_s, f"MCP tool {name!r}")


def _is_timeout(exc: BaseException, transport_timeout: type[BaseException]) -> bool:
    nested = getattr(exc, "exceptions", None)
    if nested:
        return any(_is_timeout(inner, transport_timeout) for inner in nested)
    return isinstance(exc, (asyncio.TimeoutError, transport_timeout))


def _describe(exc: BaseException) -> str:
    nested = getattr(exc, "exceptions", None)
    if nested:
        return "; ".join(_describe(inner) for inner in nested)
    return f"{type(exc).__name__}: {exc}"


def _flatten_result(result: Any) -> dict[str, Any]:
    text_parts: list[str] = []
    data: list[dict[str, Any]] = []
    for content in result.content or []:
        content_type = getattr(content, "type", None) or "unknown"
        if content_type == "text":
            text_parts.append(getattr(content, "text", "") or "")
        else:
            data.append({"type": content_type, "value": content.model_dump(mode="json", exclude_none=True)})
    text = "\n".join(part for part in text_parts if part) or None
    structured = getattr(result, "structured_content", None)
    if structured and text is None:
        data.append({"type": "structured", "value": structured})
    return {
        "is_error": bool(getattr(result, "is_error", False)),
        "text": text,
        "data": data or None,
    }
