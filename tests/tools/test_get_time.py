from unittest.mock import MagicMock
from collections.abc import Callable

import httpx
import pytest

from reachy_mini_conversation_app.tools import get_time as get_time_module
from reachy_mini_conversation_app.tools.core_tools import ToolDependencies


def _deps() -> ToolDependencies:
    return ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock())


def _host_timezone(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Point the host clock's /etc/localtime at a zoneinfo entry."""
    monkeypatch.setattr(get_time_module.os, "readlink", lambda path: f"/usr/share/zoneinfo/{name}")


def _ip_lookup(monkeypatch: pytest.MonkeyPatch, respond: Callable[[httpx.Request], httpx.Response]) -> list[str]:
    """Answer the IP timezone lookup with respond and record the requested URLs."""
    requested_urls: list[str] = []
    real_client = httpx.AsyncClient

    def handle(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return respond(request)

    monkeypatch.setattr(
        get_time_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    return requested_urls


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("offline", request=request)


@pytest.mark.asyncio
async def test_get_time_reports_an_explicit_timezone() -> None:
    """A named IANA timezone should be reported with its own offset."""
    result = await get_time_module.GetTime()(_deps(), timezone="Asia/Tokyo")

    assert result["timezone"] == "Asia/Tokyo"
    assert result["utc_offset_minutes"] == 540
    assert result["iso"].startswith(f"{result['date']}T{result['time']}")
    assert result["iso"].endswith("+09:00")


@pytest.mark.asyncio
async def test_get_time_compares_two_timezones() -> None:
    """The difference should be signed from the base timezone to the compared one."""
    result = await get_time_module.GetTime()(_deps(), timezone="Asia/Tokyo", compare_timezone="Asia/Kolkata")

    assert result["compare"]["timezone"] == "Asia/Kolkata"
    assert result["time_difference_minutes"] == -210
    assert result["time_difference_hours"] == -3.5
    assert result["time_difference_summary"] == "Asia/Kolkata is 3 hours 30 minutes behind Asia/Tokyo."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"timezone": "Mars/Olympus_Mons"},
        {"timezone": "Canada"},
        {"timezone": "Asia/Tokyo", "compare_timezone": "../etc/passwd"},
    ],
)
async def test_get_time_rejects_unknown_timezones(arguments: dict[str, str]) -> None:
    """An invalid timezone name should come back as a tool error."""
    result = await get_time_module.GetTime()(_deps(), **arguments)

    assert list(result) == ["error"]


@pytest.mark.asyncio
async def test_get_time_uses_a_configured_host_timezone_without_a_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local time should come straight from a host clock that has a timezone set."""
    _host_timezone(monkeypatch, "Asia/Tokyo")
    requested_urls = _ip_lookup(monkeypatch, _unreachable)

    result = await get_time_module.GetTime()(_deps())

    assert result["timezone"] == "Asia/Tokyo"
    assert result["utc_offset_minutes"] == 540
    assert "note" not in result
    assert requested_urls == []


@pytest.mark.asyncio
async def test_get_time_locates_an_unset_robot_clock_by_ip_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A robot still on the image's default timezone should be located by IP, and only once."""
    _host_timezone(monkeypatch, "Europe/London")
    requested_urls = _ip_lookup(
        monkeypatch,
        lambda request: httpx.Response(200, json={"success": True, "timezone": {"id": "Asia/Tokyo"}}),
    )
    tool = get_time_module.GetTime()

    first = await tool(_deps())
    second = await tool(_deps(), timezone="")

    assert first["timezone"] == second["timezone"] == "Asia/Tokyo"
    assert first["utc_offset_minutes"] == 540
    assert "note" not in first
    assert len(requested_urls) == 1
    assert requested_urls[0].startswith("https://ipwho.is/")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "respond",
    [_unreachable, lambda request: httpx.Response(200, json={"success": False, "message": "rate limited"})],
)
async def test_get_time_flags_an_unset_clock_when_the_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
    respond: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Without a location, an unset clock's time must be returned with a warning instead of passed off as local."""
    _host_timezone(monkeypatch, "Etc/UTC")
    _ip_lookup(monkeypatch, respond)

    result = await get_time_module.GetTime()(_deps())

    assert result["timezone"] == "Etc/UTC"
    assert result["utc_offset_minutes"] == 0
    assert "may not be the user's local time" in result["note"]
