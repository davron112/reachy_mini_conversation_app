import os
import asyncio
import logging
from time import monotonic
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)

_IP_TIMEZONE_URL = "https://ipwho.is/?fields=success,timezone"
_IP_LOOKUP_TIMEOUT_S = 3.0
_TIMEZONE_CACHE_TTL_S = 3600.0
_TIMEZONE_RETRY_DELAY_S = 60.0


def _load_timezone(name: str) -> ZoneInfo | None:
    """Return the IANA timezone with this name, or None when it is unknown."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        # OSError: region folders like "Canada" or "US" exist in the zoneinfo tree but are not zones.
        return None


def _offset_minutes(now: datetime) -> int:
    offset = now.utcoffset()
    return int(offset.total_seconds() // 60) if offset else 0


def _format_time(now: datetime, timezone_name: str) -> dict[str, object]:
    return {
        "iso": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "weekday": now.strftime("%A"),
        "timezone": timezone_name,
        "utc_offset_minutes": _offset_minutes(now),
        "summary": now.strftime("%A, %d %B %Y, %H:%M"),
    }


class GetTime(Tool):
    """Report the current date and time, optionally compared with a second timezone."""

    name = "get_time"
    description = (
        "Get the current date and time for an IANA timezone, and optionally the difference to a second timezone. "
        "Call this directly whenever the user asks what time it is or the time somewhere. Pass an IANA name like "
        "'Europe/Paris' or 'Asia/Tokyo' for a named place (derive it from the place), or leave the timezone empty "
        "to estimate local time from the app host's public IP. An explicit process timezone takes precedence. "
        "If the result includes a note, explain that caveat to the user."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "default": "",
                "description": "IANA timezone like 'Europe/Paris'. Empty estimates local time from the app host's public IP.",
            },
            "compare_timezone": {
                "type": "string",
                "default": "",
                "description": "Optional second IANA timezone to compare against.",
            },
        },
        "required": [],
    }

    def __init__(self) -> None:
        """Keep timezone lookups cached and serialize concurrent requests."""
        self._located_timezone: ZoneInfo | None = None
        self._next_lookup_at = 0.0
        self._lookup_lock = asyncio.Lock()

    async def __call__(self, deps: ToolDependencies, **kwargs: object) -> dict[str, object]:
        """Return the time in the requested or local timezone, with an optional comparison."""
        timezone_name = str(kwargs.get("timezone") or "").strip()
        compare_name = str(kwargs.get("compare_timezone") or "").strip()
        logger.info("Tool call: get_time timezone=%s compare_timezone=%s", timezone_name, compare_name)

        compare_zone = _load_timezone(compare_name) if compare_name else None
        if compare_name and compare_zone is None:
            return {"error": f"unknown timezone '{compare_name}'; pass an IANA name like 'Asia/Tokyo'"}

        note = None
        zone: ZoneInfo | None = None
        if timezone_name:
            zone = _load_timezone(timezone_name)
            if zone is None:
                return {"error": f"unknown timezone '{timezone_name}'; pass an IANA name like 'Europe/Paris'"}
        elif process_timezone := os.environ.get("TZ"):
            zone = _load_timezone(process_timezone.removeprefix(":"))
            timezone_name = zone.key if zone is not None else ""
        else:
            async with self._lookup_lock:
                if monotonic() >= self._next_lookup_at:
                    self._located_timezone = None
                    try:
                        async with httpx.AsyncClient(timeout=_IP_LOOKUP_TIMEOUT_S) as client:
                            response = await client.get(_IP_TIMEZONE_URL)
                            response.raise_for_status()
                            payload: object = response.json()
                    except (httpx.HTTPError, ValueError) as exc:
                        logger.warning("Could not resolve local timezone from the public IP: %s", exc)
                    else:
                        timezone = (
                            payload.get("timezone") if isinstance(payload, dict) and payload.get("success") else None
                        )
                        located_name = timezone.get("id") if isinstance(timezone, dict) else None
                        if isinstance(located_name, str):
                            self._located_timezone = _load_timezone(located_name)
                        if self._located_timezone is None:
                            logger.warning("IP timezone lookup returned no valid IANA timezone")
                    cache_duration = (
                        _TIMEZONE_CACHE_TTL_S if self._located_timezone is not None else _TIMEZONE_RETRY_DELAY_S
                    )
                    self._next_lookup_at = monotonic() + cache_duration
                zone = self._located_timezone
            if zone is not None:
                timezone_name = zone.key
                note = "Timezone estimated from the app host's public IP; a VPN or proxy may place it elsewhere."
            else:
                note = (
                    "Could not determine the local timezone from the public IP. This is the app host's time, "
                    "which may not be the user's local time. Pass an IANA timezone for a specific location."
                )

        now_utc = datetime.now(UTC)
        base_now = now_utc.astimezone(zone)
        timezone_name = timezone_name or base_now.tzname() or "local"

        result = _format_time(base_now, timezone_name)
        if note is not None:
            result["note"] = note

        if compare_zone is not None:
            compare_now = now_utc.astimezone(compare_zone)
            difference_minutes = _offset_minutes(compare_now) - _offset_minutes(base_now)
            if difference_minutes == 0:
                difference_summary = f"{compare_name} has the same current UTC offset as {timezone_name}."
            else:
                hours, minutes = divmod(abs(difference_minutes), 60)
                duration = f"{hours} hour{'s' if hours != 1 else ''}"
                if minutes:
                    duration = f"{duration} {minutes} minute{'s' if minutes != 1 else ''}"
                direction = "ahead of" if difference_minutes > 0 else "behind"
                difference_summary = f"{compare_name} is {duration} {direction} {timezone_name}."
            result["compare"] = _format_time(compare_now, compare_name)
            result["time_difference_minutes"] = difference_minutes
            result["time_difference_hours"] = difference_minutes / 60
            result["time_difference_summary"] = difference_summary

        return result
