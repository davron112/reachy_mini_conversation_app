"""LLM-callable tool: set or adjust the music player volume (0..100)."""

from __future__ import annotations
import logging
from typing import Any, Dict

from reachy_mini_conversation_app.music.player import MAX_VOLUME, MIN_VOLUME
from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)


class MusicVolume(Tool):
    """Adjust the master volume of the music player."""

    name = "music_volume"
    description = (
        "Adjust music volume on a 0..100 scale. Pass `level` to set an absolute "
        "value, or `delta` (positive to raise, negative to lower) to nudge from "
        "the current volume. Exactly one of `level` or `delta` is required."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "level": {
                "type": "integer",
                "minimum": MIN_VOLUME,
                "maximum": MAX_VOLUME,
                "description": "Absolute target volume between 0 (mute) and 100 (max).",
            },
            "delta": {
                "type": "integer",
                "minimum": -MAX_VOLUME,
                "maximum": MAX_VOLUME,
                "description": "Relative volume change. Positive = louder, negative = quieter.",
            },
        },
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> Dict[str, Any]:
        """Apply an absolute level or a relative delta to the player."""
        session = deps.music_session
        if session is None:
            return {"error": "Music subsystem is not available"}

        raw_level = kwargs.get("level")
        raw_delta = kwargs.get("delta")

        if raw_level is None and raw_delta is None:
            return {"error": "either `level` or `delta` is required"}
        if raw_level is not None and raw_delta is not None:
            return {"error": "pass either `level` or `delta`, not both"}

        try:
            if raw_level is not None:
                target = int(raw_level)
            else:
                current = await session.get_volume()
                target = current + int(raw_delta)
        except (TypeError, ValueError):
            return {"error": "volume values must be integers"}

        target = max(MIN_VOLUME, min(MAX_VOLUME, target))

        try:
            applied = await session.set_volume(target)
            return {"status": "ok", "volume": applied}
        except Exception as exc:  # noqa: BLE001
            logger.exception("music_volume failed")
            return {"error": str(exc)}
