"""LLM-callable tool: search and start playback for a user query."""

from __future__ import annotations
import logging
from typing import Any, Dict

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)


class PlayMusic(Tool):
    """Search and play music from the configured backend."""

    name = "play_music"
    description = (
        "Search for music and start playback. "
        "Pass `query` with an artist, song, mood or playlist name. "
        "Use `station='personal'` or `station='kids'` to start a curated radio without a query."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Free-text search: 'Imagine Dragons Believer', 'calm jazz', etc.",
            },
            "station": {
                "type": "string",
                "enum": ["personal", "kids"],
                "description": "Start a curated station instead of searching by query.",
            },
        },
        "required": [],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> Dict[str, Any]:
        """Dispatch to query, personal station, or kids station."""
        session = deps.music_session
        if session is None:
            return {"error": "Music subsystem is not available"}
        try:
            station = kwargs.get("station")
            if station == "personal":
                track = await session.play_personal_station()
            elif station == "kids":
                track = await session.play_kids_station()
            else:
                query = (kwargs.get("query") or "").strip()
                if not query:
                    return {"error": "Provide `query` or `station` (personal/kids)"}
                track = await session.play_query(query)
            return {
                "status": "playing",
                "track": track.display_name(),
                "source": track.source,
                "queue_length": len(session.queue),
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("play_music failed")
            return {"error": str(exc)}
