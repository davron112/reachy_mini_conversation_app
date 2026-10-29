"""LLM-callable tool: pause/resume/skip/stop the current music playback."""

from __future__ import annotations
import logging
from typing import Any, Dict

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)

ACTIONS = ("pause", "resume", "next", "stop")


class MusicControl(Tool):
    """Dispatcher for transport actions on the current music session."""

    name = "music_control"
    description = (
        "Control the currently-playing music. "
        "Actions: 'pause', 'resume', 'next' (skip to next queued track), 'stop' (clear queue)."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(ACTIONS),
                "description": "Transport action to apply to the current music session.",
            },
        },
        "required": ["action"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> Dict[str, Any]:
        """Dispatch on the requested action."""
        session = deps.music_session
        if session is None:
            return {"error": "Music subsystem is not available"}
        action = (kwargs.get("action") or "").strip().lower()
        if action not in ACTIONS:
            return {"error": f"Unknown action {action!r}. Expected one of: {ACTIONS}"}
        try:
            if action == "pause":
                await session.pause()
                return {"status": "paused"}
            if action == "resume":
                await session.resume()
                return {"status": "playing"}
            if action == "stop":
                await session.stop()
                return {"status": "stopped"}
            if action == "next":
                nxt = await session.next_track()
                if nxt is None:
                    return {"status": "stopped", "reason": "queue empty"}
                return {"status": "playing", "track": nxt.display_name()}
        except Exception as exc:  # noqa: BLE001
            logger.exception("music_control(%s) failed", action)
            return {"error": str(exc)}
        return {"error": "unreachable"}
