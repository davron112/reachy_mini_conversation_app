import sys
import time
import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock
from collections.abc import Callable

import httpx
import pytest

from reachy_mini_conversation_app.tools import get_time as get_time_module
from reachy_mini_conversation_app.tools.core_tools import ToolDependencies


_HTTP_CLIENT = httpx.AsyncClient


@pytest.fixture(autouse=True)
def _isolate_network_and_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(httpx, "AsyncClient", MagicMock(side_effect=AssertionError("Unexpected network access")))


def _deps() -> ToolDependencies:
    return ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock())


def _host_timezone(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    instant = datetime(2026, 7, 1, 12, tzinfo=UTC)
    host_zone = get_time_module.ZoneInfo(name)
    host_clock = MagicMock(wraps=instant)
    host_clock.astimezone.side_effect = lambda zone=None: instant.astimezone(host_zone if zone is None else zone)
    monkeypatch.setattr(get_time_module, "datetime", MagicMock(now=MagicMock(return_value=host_clock)))


def _ip_lookup(
    monkeypatch: pytest.MonkeyPatch,
    respond: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await asyncio.sleep(0)
        return respond(request)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: _HTTP_CLIENT(transport=httpx.MockTransport(handle), **kwargs),
    )
    return requests


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
@pytest.mark.parametrize("host_timezone", ["UTC", "GMT", "Europe/London", "Asia/Tokyo"])
async def test_get_time_resolves_local_time_without_configuring_the_host(
    monkeypatch: pytest.MonkeyPatch,
    host_timezone: str,
) -> None:
    """An empty timezone should resolve the caller's location instead of trusting the image's clock setting."""
    _host_timezone(monkeypatch, host_timezone)
    requests = _ip_lookup(
        monkeypatch,
        lambda request: httpx.Response(200, json={"success": True, "timezone": {"id": "Europe/Zurich"}}),
    )

    result = await get_time_module.GetTime()(_deps(), compare_timezone="Asia/Tokyo")

    assert result["timezone"] == "Europe/Zurich"
    assert result["time"] == "14:00"
    assert result["utc_offset_minutes"] == 120
    assert result["time_difference_minutes"] == 420
    assert "public IP" in result["note"]
    assert "VPN" in result["note"]
    assert len(requests) == 1
    assert str(requests[0].url) == "https://ipwho.is/?fields=success,timezone"
    assert "authorization" not in requests[0].headers


@pytest.mark.asyncio
@pytest.mark.parametrize("timezone", ["Europe/London", "UTC", ":Asia/Tokyo"])
async def test_get_time_honors_an_explicit_process_timezone_without_lookup(
    monkeypatch: pytest.MonkeyPatch,
    timezone: str,
) -> None:
    """A deliberately supplied process timezone should take precedence over IP inference."""
    monkeypatch.setenv("TZ", timezone)
    expected_zone = get_time_module.ZoneInfo(timezone.removeprefix(":"))

    result = await get_time_module.GetTime()(_deps())

    assert result["timezone"] == expected_zone.key
    assert "note" not in result


@pytest.mark.asyncio
async def test_get_time_argument_takes_precedence_over_the_process_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A named location should override the process timezone without making a lookup."""
    monkeypatch.setenv("TZ", "UTC")

    result = await get_time_module.GetTime()(_deps(), timezone="Asia/Tokyo")

    assert result["timezone"] == "Asia/Tokyo"
    assert result["utc_offset_minutes"] == 540
    assert "note" not in result


@pytest.mark.asyncio
async def test_get_time_reuses_and_refreshes_the_located_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local queries should share cached location until its one-hour lifetime expires."""
    _host_timezone(monkeypatch, "Europe/London")
    clock = MagicMock(return_value=1000.0)
    monkeypatch.setattr(get_time_module, "monotonic", clock)
    requests = _ip_lookup(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"success": True, "timezone": {"id": "Europe/Zurich" if clock() < 4600 else "Asia/Tokyo"}}
        ),
    )
    tool = get_time_module.GetTime()

    first = await tool(_deps())
    get_time_module.datetime.now.return_value = datetime(2026, 7, 1, 12, 5, tzinfo=UTC)
    cached = await tool(_deps())
    assert first["time"] == "14:00"
    assert cached["time"] == "14:05"
    assert first["timezone"] == cached["timezone"] == "Europe/Zurich"
    assert len(requests) == 1

    clock.return_value = 4601.0
    refreshed = await tool(_deps())
    assert refreshed["timezone"] == "Asia/Tokyo"
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_get_time_shares_one_lookup_between_concurrent_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Overlapping local-time requests should not duplicate the location lookup."""
    requests = _ip_lookup(
        monkeypatch,
        lambda request: httpx.Response(200, json={"success": True, "timezone": {"id": "Europe/Zurich"}}),
    )
    tool = get_time_module.GetTime()

    results = await asyncio.gather(tool(_deps()), tool(_deps()))

    assert all(result["timezone"] == "Europe/Zurich" for result in results)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_get_time_cancellation_does_not_block_later_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancelled lookup should release its lock so the next request can resolve local time."""
    requests = _ip_lookup(
        monkeypatch,
        lambda request: httpx.Response(200, json={"success": True, "timezone": {"id": "Europe/Zurich"}}),
    )
    tool = get_time_module.GetTime()
    pending = asyncio.create_task(tool(_deps()))
    await asyncio.sleep(0)
    assert len(requests) == 1
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    result = await asyncio.wait_for(tool(_deps()), timeout=1.0)

    assert result["timezone"] == "Europe/Zurich"
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "respond",
    [
        _unreachable,
        MagicMock(side_effect=httpx.ReadTimeout("lookup timed out")),
        lambda request: httpx.Response(503),
        lambda request: httpx.Response(200, text="not JSON"),
        lambda request: httpx.Response(200, json=[]),
        lambda request: httpx.Response(200, json={"success": False}),
        lambda request: httpx.Response(200, json={"success": True, "timezone": {"id": "Mars/Olympus_Mons"}}),
    ],
)
async def test_get_time_falls_back_with_a_warning_when_location_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    respond: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Failed lookups should report host time as a fallback, log the failure, and suppress immediate retries."""
    _host_timezone(monkeypatch, "GMT")
    requests = _ip_lookup(monkeypatch, respond)
    tool = get_time_module.GetTime()

    first = await tool(_deps())
    second = await tool(_deps())

    assert first["timezone"] == second["timezone"] == "GMT"
    assert first["utc_offset_minutes"] == 0
    assert "may not be the user's local time" in first["note"]
    assert len(requests) == 1
    assert caplog.records


@pytest.mark.asyncio
async def test_get_time_recovers_after_a_failed_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A temporary location failure should retry after one minute and then return the inferred local time."""
    _host_timezone(monkeypatch, "UTC")
    clock = MagicMock(return_value=1000.0)
    monkeypatch.setattr(get_time_module, "monotonic", clock)
    requests = _ip_lookup(monkeypatch, _unreachable)
    tool = get_time_module.GetTime()

    fallback = await tool(_deps())
    assert fallback["timezone"] == "UTC"
    assert "Could not determine" in fallback["note"]

    recovered_requests = _ip_lookup(
        monkeypatch,
        lambda request: httpx.Response(200, json={"success": True, "timezone": {"id": "Europe/Zurich"}}),
    )
    clock.return_value = 1059.0
    await tool(_deps())
    assert recovered_requests == []

    clock.return_value = 1061.0
    recovered = await tool(_deps())
    assert recovered["timezone"] == "Europe/Zurich"
    assert recovered["time"] == "14:00"
    assert len(requests) == len(recovered_requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("timezone", ["UTC", "Europe/London"])
async def test_get_time_does_not_qualify_explicit_timezones(timezone: str) -> None:
    """An explicitly requested timezone should not be treated as an uncertain host default."""
    result = await get_time_module.GetTime()(_deps(), timezone=timezone)

    assert result["timezone"] == timezone
    assert "note" not in result


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX TZ overrides are not supported on Windows")
async def test_get_time_honors_a_process_timezone_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """A process timezone override should apply without changing system configuration."""
    try:
        with monkeypatch.context() as environment:
            environment.setenv("TZ", "Asia/Tokyo")
            time.tzset()
            result = await get_time_module.GetTime()(_deps())
    finally:
        time.tzset()

    assert result["utc_offset_minutes"] == 540
    assert result["timezone"] == "Asia/Tokyo"
