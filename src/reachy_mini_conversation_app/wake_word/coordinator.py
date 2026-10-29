"""React to wake-word detections by ducking music and opening an active window.

An attention move is signalled on wake, and the music volume is restored when the
active window ends.

Music is ducked (volume lowered), never paused: ducking is orthogonal to
transport, so explicit user commands during the window (pause/stop/next/play)
are never overridden by an automatic resume.
"""

from __future__ import annotations
import os
import time
import asyncio
import logging
from typing import Any, Callable, Optional, Awaitable


logger = logging.getLogger(__name__)


ENV_ACTIVE_WINDOW_S = "REACHY_WAKE_ACTIVE_WINDOW_S"
DEFAULT_ACTIVE_WINDOW_S = 12.0


class WakeWordCoordinator:
    """Glue between the wake-word detector and the rest of the app."""

    def __init__(
        self,
        music_session: Any | None = None,
        attention_cue: Optional[Callable[[], Awaitable[None]]] = None,
        active_window_s: float | None = None,
    ) -> None:
        """Configure collaborators and the active-window length (env override when not given)."""
        self._music_session = music_session
        self._attention_cue = attention_cue

        if active_window_s is None:
            raw = os.getenv(ENV_ACTIVE_WINDOW_S)
            try:
                active_window_s = float(raw) if raw else DEFAULT_ACTIVE_WINDOW_S
            except ValueError:
                logger.warning("Invalid %s=%r, using default", ENV_ACTIVE_WINDOW_S, raw)
                active_window_s = DEFAULT_ACTIVE_WINDOW_S
        self._active_window_s = active_window_s

        self._lock = asyncio.Lock()
        self._active_until = 0.0
        self._resume_task: Optional[asyncio.Task[None]] = None

    @property
    def is_active(self) -> bool:
        """Return whether the post-wake active window is still open."""
        return time.monotonic() < self._active_until

    async def on_wake(self, label: str, score: float) -> None:
        """Entry point called by WakeWordDetector when the wake word fires."""
        async with self._lock:
            extended = self.is_active
            self._active_until = time.monotonic() + self._active_window_s
            if extended:
                logger.info("Wake-word re-fired (%s, %.2f); extending active window", label, score)
                return

            logger.info("Wake-word activated (%s, %.2f); ducking music + cue", label, score)
            await self._enter_active_state()
            if self._resume_task and not self._resume_task.done():
                self._resume_task.cancel()
            self._resume_task = asyncio.create_task(self._resume_after_window())

    async def _enter_active_state(self) -> None:
        if self._music_session is not None:
            try:
                await self._music_session.duck()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to duck music on wake: %s", exc)

        if self._attention_cue is not None:
            try:
                await self._attention_cue()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Attention cue failed: %s", exc)

    async def _resume_after_window(self) -> None:
        try:
            while True:
                remaining = self._active_until - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            return

        await self._exit_active_state()

    async def _exit_active_state(self) -> None:
        logger.info("Wake-word window ended; restoring music volume")
        if self._music_session is not None:
            try:
                await self._music_session.unduck()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to restore music volume after wake window: %s", exc)
