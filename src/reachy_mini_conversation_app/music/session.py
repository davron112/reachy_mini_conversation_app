"""MusicSession: glues a MusicBackend and an AudioPlayer together with a queue."""

from __future__ import annotations
import os
import logging
from typing import Literal
from pathlib import Path

from reachy_mini_conversation_app.music.types import TrackInfo
from reachy_mini_conversation_app.music.player import (
    MAX_VOLUME,
    MIN_VOLUME,
    AudioPlayer,
    AudioPlayerError,
    select_audio_player,
)
from reachy_mini_conversation_app.music.backends import (
    LocalBackend,
    MusicBackend,
    YandexBackend,
    MusicBackendError,
)


logger = logging.getLogger(__name__)

ENV_BACKEND = "REACHY_MUSIC_BACKEND"
ENV_YANDEX_TOKEN = "YANDEX_MUSIC_TOKEN"
ENV_LOCAL_MUSIC_DIR = "REACHY_LOCAL_MUSIC_DIR"
ENV_DUCK_VOLUME = "REACHY_MUSIC_DUCK_VOLUME"
ENV_MUSIC_VOLUME = "REACHY_MUSIC_VOLUME"

DUCK_VOLUME_DEFAULT = 10
MUSIC_VOLUME_DEFAULT = 65

BackendName = Literal["auto", "yandex", "local"]


class MusicSessionError(RuntimeError):
    """Raised when the music session cannot satisfy a request."""


class MusicSession:
    """Holds a backend + player + queue, exposes high-level controls for tools."""

    def __init__(
        self,
        backend: MusicBackend,
        player: AudioPlayer,
        duck_volume: int = DUCK_VOLUME_DEFAULT,
        default_volume: int = MUSIC_VOLUME_DEFAULT,
    ) -> None:
        """Wire a backend and player together with default volume targets."""
        self._backend = backend
        self._player = player
        self._duck_volume = max(MIN_VOLUME, min(MAX_VOLUME, duck_volume))
        self._default_volume = max(MIN_VOLUME, min(MAX_VOLUME, default_volume))
        self._queue: list[TrackInfo] = []
        self._current: TrackInfo | None = None
        self._pre_duck_volume: int | None = None

    @property
    def backend_name(self) -> str:
        """Return the active backend's identifier (yandex, local, ...)."""
        return self._backend.name

    @property
    def player_name(self) -> str:
        """Return the active audio player identifier (mpv, vlc, ...)."""
        return self._player.name

    @property
    def current_track(self) -> TrackInfo | None:
        """Return the currently loaded track, if any."""
        return self._current

    @property
    def queue(self) -> list[TrackInfo]:
        """Return the upcoming queue (read-only view)."""
        return list(self._queue)

    async def search(self, query: str, limit: int = 5) -> list[TrackInfo]:
        """Search the configured backend."""
        return await self._backend.search(query, limit=limit)

    async def play_query(self, query: str) -> TrackInfo:
        """Search and play the top result; queue the remaining results."""
        results = await self.search(query, limit=5)
        if not results:
            raise MusicSessionError(f"No tracks found for: {query!r}")
        head, *rest = results
        self._queue = list(rest)
        await self._play_track(head)
        return head

    async def play_personal_station(self) -> TrackInfo:
        """Start the user's personal radio (or first local track if backend is local)."""
        tracks = await self._backend.personal_station()
        if not tracks:
            raise MusicSessionError("Personal station returned no tracks")
        head, *rest = tracks
        self._queue = list(rest)
        await self._play_track(head)
        return head

    async def play_kids_station(self) -> TrackInfo:
        """Start the kids-friendly station."""
        tracks = await self._backend.kids_station()
        if not tracks:
            raise MusicSessionError("Kids station returned no tracks")
        head, *rest = tracks
        self._queue = list(rest)
        await self._play_track(head)
        return head

    async def next_track(self) -> TrackInfo | None:
        """Advance to the next queued track; return None when the queue is empty."""
        if not self._queue:
            await self.stop()
            return None
        nxt = self._queue.pop(0)
        await self._play_track(nxt)
        return nxt

    async def pause(self) -> None:
        """Pause the current track."""
        await self._player.pause()

    async def resume(self) -> None:
        """Resume a paused track."""
        await self._player.resume()

    async def stop(self) -> None:
        """Stop playback and forget the current track."""
        await self._player.stop()
        self._current = None
        self._queue = []

    async def set_volume(self, level: int) -> int:
        """Set master volume and return the clamped value."""
        clamped = max(MIN_VOLUME, min(MAX_VOLUME, level))
        await self._player.set_volume(clamped)
        self._default_volume = clamped
        return clamped

    async def get_volume(self) -> int:
        """Return the current player volume."""
        return await self._player.get_volume()

    async def is_playing(self) -> bool:
        """Return True when a track is actively playing."""
        return await self._player.is_playing()

    async def duck(self) -> None:
        """Drop music volume while the assistant is speaking."""
        if self._current is None or self._pre_duck_volume is not None:
            return
        try:
            self._pre_duck_volume = await self._player.get_volume()
            await self._player.set_volume(self._duck_volume)
        except AudioPlayerError:
            self._pre_duck_volume = None

    async def unduck(self) -> None:
        """Restore the pre-duck volume after the assistant finishes speaking."""
        if self._pre_duck_volume is None:
            return
        try:
            await self._player.set_volume(self._pre_duck_volume)
        except AudioPlayerError:
            pass
        finally:
            self._pre_duck_volume = None

    async def close(self) -> None:
        """Release the player process."""
        await self._player.close()

    def terminate(self) -> None:
        """Release the player process synchronously, for app shutdown."""
        self._player.terminate()

    async def _play_track(self, track: TrackInfo) -> None:
        try:
            url = track.stream_url or await self._backend.resolve_stream_url(track)
        except MusicBackendError as exc:
            raise MusicSessionError(f"Cannot resolve stream URL for {track.display_name()}: {exc}") from exc
        await self._player.play(url)
        await self._player.set_volume(self._default_volume)
        self._current = track


def _resolve_local_root() -> Path:
    raw = os.getenv(ENV_LOCAL_MUSIC_DIR)
    if raw:
        return Path(raw).expanduser()
    return Path.home() / "Music" / "reachy_mini"


def _build_backend(name: BackendName) -> MusicBackend:
    if name == "yandex":
        token = os.getenv(ENV_YANDEX_TOKEN, "").strip()
        if not token:
            raise MusicSessionError(f"{ENV_YANDEX_TOKEN} is not set. Get a Yandex OAuth token and add it to .env.")
        return YandexBackend(token=token)
    if name == "local":
        return LocalBackend(root=_resolve_local_root())
    raise MusicSessionError(f"Unknown music backend: {name}")


def _select_backend() -> MusicBackend:
    requested = (os.getenv(ENV_BACKEND, "auto") or "auto").strip().lower()
    if requested in {"yandex", "local"}:
        return _build_backend(requested)  # type: ignore[arg-type]
    if os.getenv(ENV_YANDEX_TOKEN, "").strip():
        try:
            return _build_backend("yandex")
        except MusicSessionError as exc:
            logger.warning("Yandex backend unavailable, falling back to local: %s", exc)
    return _build_backend("local")


def build_music_session() -> MusicSession | None:
    """Build a MusicSession using env-configured backend and the best available player.

    Returns None when no playable backend can be initialised; callers should
    treat that as 'music disabled' rather than fatal.
    """
    try:
        backend = _select_backend()
    except MusicSessionError as exc:
        logger.warning("Music backend not available: %s", exc)
        return None
    try:
        player = select_audio_player()
    except AudioPlayerError as exc:
        logger.warning("Audio player not available: %s", exc)
        return None

    duck_raw = os.getenv(ENV_DUCK_VOLUME)
    try:
        duck = int(duck_raw) if duck_raw else DUCK_VOLUME_DEFAULT
    except ValueError:
        logger.warning("Invalid %s=%r, using default", ENV_DUCK_VOLUME, duck_raw)
        duck = DUCK_VOLUME_DEFAULT

    volume_raw = os.getenv(ENV_MUSIC_VOLUME)
    try:
        default_volume = int(volume_raw) if volume_raw else MUSIC_VOLUME_DEFAULT
    except ValueError:
        logger.warning("Invalid %s=%r, using default", ENV_MUSIC_VOLUME, volume_raw)
        default_volume = MUSIC_VOLUME_DEFAULT

    session = MusicSession(
        backend=backend,
        player=player,
        duck_volume=duck,
        default_volume=default_volume,
    )
    logger.info("Music session ready: backend=%s player=%s", backend.name, player.name)
    return session
