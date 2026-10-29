"""Async MCP client over HTTP SSE transport with auth.

Each call opens a short-lived SSE session, runs the request, then closes. This
avoids binding the anyio task group to a specific event loop, which matters because
discovery happens in one loop and tool dispatch happens in Gradio's loop.

Latency overhead per call is the SSE handshake (~50-200ms LAN), acceptable for the
kinds of tools an MCP server typically exposes (search, email, calendar).
"""

from __future__ import annotations
import logging
from typing import Any
from contextlib import AsyncExitStack


logger = logging.getLogger(__name__)

MCP_REQUEST_TIMEOUT_S = 30.0


class MCPBridgeError(RuntimeError):
    """Raised when the MCP bridge cannot satisfy a request."""


class MCPSession:
    """Short-lived MCP client over SSE; one session per RPC call."""

    def __init__(self, url: str, api_key: str | None = None, extra_headers: dict[str, str] | None = None) -> None:
        """Store connection params; sessions are opened on demand."""
        if not url:
            raise MCPBridgeError("MCP server URL is required")
        self._url = url
        self._api_key = api_key
        self._extra_headers = dict(extra_headers or {})

    @property
    def url(self) -> str:
        """Return the configured server URL."""
        return self._url

    def _headers(self) -> dict[str, str]:
        headers = dict(self._extra_headers)
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    @staticmethod
    def _load_sdk():  # type: ignore[no-untyped-def]
        try:
            from mcp import ClientSession  # type: ignore[import-not-found]
            from mcp.client.sse import sse_client  # type: ignore[import-not-found]
        except ImportError as exc:
            raise MCPBridgeError(
                "mcp package not installed. Install with: pip install 'mcp[cli]>=1.0'"
            ) from exc
        return ClientSession, sse_client

    async def _open(self, stack: AsyncExitStack):  # type: ignore[no-untyped-def]
        ClientSession, sse_client = self._load_sdk()
        read_stream, write_stream = await stack.enter_async_context(
            sse_client(self._url, headers=self._headers(), timeout=MCP_REQUEST_TIMEOUT_S)
        )
        session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
        await session.initialize()
        return session

    async def list_tools(self) -> list[dict[str, Any]]:
        """One-shot list_tools call. Each entry has name/description/inputSchema."""
        async with AsyncExitStack() as stack:
            session = await self._open(stack)
            response = await session.list_tools()
            return [
                {
                    "name": t.name,
                    "description": (t.description or "").strip(),
                    "inputSchema": t.inputSchema or {"type": "object", "properties": {}},
                }
                for t in response.tools
            ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """One-shot tool call. Flattens the MCP content list into text + structured data."""
        async with AsyncExitStack() as stack:
            try:
                session = await self._open(stack)
                result = await session.call_tool(name, arguments=arguments)
            except MCPBridgeError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise MCPBridgeError(f"MCP tool call failed for {name!r}: {exc}") from exc

            text_parts: list[str] = []
            structured: list[dict[str, Any]] = []
            for content in result.content or []:
                content_type = getattr(content, "type", None)
                if content_type == "text":
                    text_parts.append(getattr(content, "text", ""))
                elif content_type in {"json", "resource"}:
                    payload = getattr(content, "json", None) or getattr(content, "resource", None)
                    if payload is not None:
                        structured.append({"type": content_type, "value": payload})
                else:
                    structured.append({"type": content_type or "unknown", "raw": str(content)})

            return {
                "is_error": bool(getattr(result, "isError", False)),
                "text": "\n".join(p for p in text_parts if p) or None,
                "data": structured or None,
            }

    async def close(self) -> None:
        """No persistent state to release; provided for interface symmetry."""
        return None
