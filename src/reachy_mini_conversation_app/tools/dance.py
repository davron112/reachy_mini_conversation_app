import random
import logging
from typing import Any, Dict

from reachy_mini_conversation_app.tools.core_tools import Tool, ToolDependencies


logger = logging.getLogger(__name__)

DANCE_REPEAT_MIN = 1
DANCE_REPEAT_MAX = 10
DANCE_REPEAT_DEFAULT = 1
DANCE_SPEED_DEFAULT = "normal"
DANCE_SLOW_BPM = 70
DANCE_FAST_BPM = 135

# Initialize dance library
try:
    from reachy_mini_dances_library.dance_move import DanceMove
    from reachy_mini_dances_library.collection.dance import AVAILABLE_MOVES
    from reachy_mini_conversation_app.dance_emotion_moves import DanceQueueMove

    DANCE_SPEED_BPM: Dict[str, float] = {
        "slow": DANCE_SLOW_BPM,
        "normal": DanceMove.default_bpm,
        "fast": DANCE_FAST_BPM,
    }
    DANCE_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Dance library not available: {e}")
    AVAILABLE_MOVES = {}
    DANCE_SPEED_BPM = {}
    DANCE_AVAILABLE = False


def get_available_dances_and_descriptions() -> str:
    """Get formatted list of available dances with descriptions."""
    if not DANCE_AVAILABLE:
        return "Moves not available."

    if not AVAILABLE_MOVES:
        return "Moves not available."

    output = ""
    for move_name, (func, params, metadata) in AVAILABLE_MOVES.items():
        description = metadata.get("description", "No description available.")
        output += f"{move_name}: {description}\n"
    return output


class Dance(Tool):
    """Play a named or random dance move at a chosen speed, replacing any dance in progress. Non-blocking."""

    name = "dance"
    description = (
        "Dance now: play a named or random dance move (optionally repeated) at a slow, normal or fast speed. "
        "Replaces any dance in progress, so call it again to change the move or speed."
    )
    needs_response = False
    parameters_schema = {
        "type": "object",
        "properties": {
            "move": {
                "type": "string",
                "enum": list(AVAILABLE_MOVES.keys() if DANCE_AVAILABLE else []),
                "description": f"""Name of the moves and their descriptions; omit for random.
                                Here is a list of the available moves, you MUST only choose from these: \n
                                {get_available_dances_and_descriptions()}
                                """,
            },
            "repeat": {
                "type": "integer",
                "minimum": DANCE_REPEAT_MIN,
                "maximum": DANCE_REPEAT_MAX,
                "description": (
                    f"How many times to repeat the move (default {DANCE_REPEAT_DEFAULT}, "
                    f"min {DANCE_REPEAT_MIN}, max {DANCE_REPEAT_MAX})."
                ),
            },
            "speed": {
                "type": "string",
                "enum": list(DANCE_SPEED_BPM),
                "description": (
                    f"Tempo of the dance (default {DANCE_SPEED_DEFAULT}). Use 'slow' when asked to dance slowly "
                    "or calmly (langsam), 'fast' when asked to dance faster (schneller)."
                ),
            },
        },
        "required": [],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> Dict[str, Any]:
        """Play a named or random dance move once (or repeat). Non-blocking."""
        if not DANCE_AVAILABLE:
            return {"error": "Dance system not available"}

        if not AVAILABLE_MOVES:
            return {"error": "No moves currently available"}

        move_name = kwargs.get("move")
        try:
            requested_repeat = int(kwargs.get("repeat", DANCE_REPEAT_DEFAULT))
        except (TypeError, ValueError):
            requested_repeat = DANCE_REPEAT_DEFAULT
        repeat = max(DANCE_REPEAT_MIN, min(DANCE_REPEAT_MAX, requested_repeat))
        if repeat != requested_repeat:
            logger.warning(
                "dance.repeat clamped: requested=%s -> %d (range %d..%d)",
                requested_repeat,
                repeat,
                DANCE_REPEAT_MIN,
                DANCE_REPEAT_MAX,
            )

        speed = kwargs.get("speed") or DANCE_SPEED_DEFAULT
        if speed not in DANCE_SPEED_BPM:
            return {"error": f"Unknown dance speed '{speed}'. Available: {list(DANCE_SPEED_BPM)}"}

        logger.info("Tool call: dance move=%s repeat=%d speed=%s", move_name, repeat, speed)

        if not move_name:
            move_name = random.choice(list(AVAILABLE_MOVES.keys()))

        if move_name not in AVAILABLE_MOVES:
            return {"error": f"Unknown dance move '{move_name}'. Available: {list(AVAILABLE_MOVES.keys())}"}

        movement_manager = deps.movement_manager
        movement_manager.clear_move_queue()
        for _ in range(repeat):
            movement_manager.queue_move(DanceQueueMove(move_name, bpm=DANCE_SPEED_BPM[speed]))

        return {"status": "queued", "move": move_name, "repeat": repeat, "speed": speed}
