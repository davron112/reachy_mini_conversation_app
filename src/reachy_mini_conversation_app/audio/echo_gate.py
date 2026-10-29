"""Half-duplex gate that keeps the robot from hearing its own voice."""

import os
import time
import logging
from typing import Callable, Optional


logger = logging.getLogger(__name__)

HALF_DUPLEX_ENV = "REACHY_HALF_DUPLEX"
ECHO_TAIL_ENV = "REACHY_ECHO_TAIL_S"
DEFAULT_ECHO_TAIL_S = 0.5
TRUTHY = {"1", "true", "yes", "on"}


class PlaybackEchoGate:
    """Close the mic while queued speaker audio plays, plus a short room-echo tail."""

    def __init__(self, tail_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        """Create a gate that stays closed for ``tail_s`` seconds after playback ends."""
        self._tail_s = tail_s
        self._clock = clock
        self._playback_end = 0.0

    def note_playback(self, seconds: float) -> None:
        """Account for ``seconds`` of audio handed to the speaker."""
        self._playback_end = max(self._playback_end, self._clock()) + max(0.0, seconds)

    def reset(self) -> None:
        """Reopen the mic immediately, e.g. after the playback queue was flushed."""
        self._playback_end = 0.0

    @property
    def is_open(self) -> bool:
        """Whether mic frames may be forwarded."""
        return self._clock() >= self._playback_end + self._tail_s


def build_echo_gate() -> Optional[PlaybackEchoGate]:
    """Return the configured gate, or None when half-duplex is disabled."""
    if (os.getenv(HALF_DUPLEX_ENV) or "1").strip().lower() not in TRUTHY:
        return None
    raw = os.getenv(ECHO_TAIL_ENV)
    try:
        tail_s = float(raw) if raw and raw.strip() else DEFAULT_ECHO_TAIL_S
    except ValueError:
        logger.warning("Invalid %s=%r, using %s", ECHO_TAIL_ENV, raw, DEFAULT_ECHO_TAIL_S)
        tail_s = DEFAULT_ECHO_TAIL_S
    return PlaybackEchoGate(max(0.0, tail_s))
