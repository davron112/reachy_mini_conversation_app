import os
import logging
from typing import Any
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)

# POSIX hosts link this into their zoneinfo tree; Windows has none and keeps the OS local time.
_LOCALTIME_PATH = "/etc/localtime"
# A clock nobody configured: UTC, or the Europe/London default reachy-mini-os inherits from pi-gen.
_UNSET_TIMEZONES = {"UTC", "Etc/UTC", "Europe/London"}
# Keyless lookup of the caller's own public IP.
_IP_TIMEZONE_URL = "https://ipwho.is/?fields=success,timezone"
_IP_LOOKUP_TIMEOUT_S = 3.0


def _load_timezone(name: str) -> ZoneInfo | None:
    """Return the IANA timezone with this name, or None when it is unknown."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _offset_minutes(now: datetime) -> int:
    offset = now.utcoffset()
    return int(offset.total_seconds() // 60) if offset else 0


def _format_time(now: datetime, timezone_name: str) -> dict[str, Any]:
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
        "for the user's own local time. Do not ask for their city and do not just say you'll check."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "default": "",
                "description": "IANA timezone like 'Europe/Paris'. Empty resolves the user's local time.",
            },
            "compare_timezone": {
                "type": "string",
                "default": "",
                "description": "Optional second IANA timezone to compare against.",
            },
        },
        "required": [],
    }

    _located_timezone: str | None = None

    async def _local_timezone(self) -> tuple[str, ZoneInfo | None, str | None]:
        """Name the host clock's timezone, locating the robot by IP when that clock was never configured."""
        try:
            system_name = os.readlink(_LOCALTIME_PATH).rpartition("zoneinfo/")[2]
        except OSError:
            system_name = ""
        system_zone = _load_timezone(system_name) if system_name else None
        if system_zone is None:
            # A None zone makes astimezone() use the OS local time, named by its abbreviation.
            system_name = datetime.now().astimezone().tzname() or "local"
        if system_name not in _UNSET_TIMEZONES:
            return system_name, system_zone, None

        if self._located_timezone is None:
            try:
                async with httpx.AsyncClient(timeout=_IP_LOOKUP_TIMEOUT_S) as client:
                    response = await client.get(_IP_TIMEZONE_URL)
                    response.raise_for_status()
                    payload = response.json()
            except (httpx.HTTPError, ValueError) as e:
                logger.warning("Could not locate the robot's timezone from its public IP: %s", e)
            else:
                timezone = payload.get("timezone") if isinstance(payload, dict) and payload.get("success") else None
                located_name = timezone.get("id") if isinstance(timezone, dict) else None
                if isinstance(located_name, str) and _load_timezone(located_name) is not None:
                    self._located_timezone = located_name
                else:
                    logger.warning("IP location returned no usable timezone: %s", payload)

        if self._located_timezone is None:
            note = (
                "The robot's clock has no timezone set and its location could not be looked up, "
                "so this may not be the user's local time."
            )
            return system_name, system_zone, note
        return self._located_timezone, _load_timezone(self._located_timezone), None

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        """Return the time in the requested or local timezone, with an optional comparison."""
        timezone_name = str(kwargs.get("timezone") or "").strip()
        compare_name = str(kwargs.get("compare_timezone") or "").strip()
        logger.info("Tool call: get_time timezone=%s compare_timezone=%s", timezone_name, compare_name)

        compare_zone = _load_timezone(compare_name) if compare_name else None
        if compare_name and compare_zone is None:
            return {"error": f"unknown timezone '{compare_name}'; pass an IANA name like 'Asia/Tokyo'"}

        note = None
        if timezone_name:
            zone = _load_timezone(timezone_name)
            if zone is None:
                return {"error": f"unknown timezone '{timezone_name}'; pass an IANA name like 'Europe/Paris'"}
        else:
            timezone_name, zone, note = await self._local_timezone()

        now_utc = datetime.now(UTC)
        base_now = now_utc.astimezone(zone)
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
