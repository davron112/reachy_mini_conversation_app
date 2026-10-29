"""LLM-callable tool: report what is currently playing."""

from __future__ import annotations
import logging
from typing import Any, Dict

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)


class MusicStatus(Tool):
    """Return the current playback state so the assistant can describe it."""

    name = "music_status"
    description = (
        "Report current music playback: track title, source, queue length, "
        "and whether playback is active."
    )
    parameters_schema = {"type": "object", "properties": {}, "required": []}

    async def __call__(self, deps: ToolDependencies, **_: Any) -> Dict[str, Any]:
        """Inspect the live session and return a JSON-friendly snapshot."""
        session = deps.music_session
        if session is None:
            return {"status": "disabled", "reason": "music subsystem not initialised"}
        try:
            playing = await session.is_playing()
            volume = await session.get_volume()
        except Exception as exc:  # noqa: BLE001
            logger.exception("music_status: failed to read player state")
            return {"error": str(exc)}
        track = session.current_track
        return {
            "playing": playing,
            "track": track.display_name() if track else None,
            "source": track.source if track else None,
            "backend": session.backend_name,
            "player": session.player_name,
            "queue_length": len(session.queue),
            "volume": volume,
        }
