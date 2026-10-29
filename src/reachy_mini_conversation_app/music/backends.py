"""Music backends: search and resolve stream URLs from a source."""

from __future__ import annotations
import logging
from typing import Protocol, runtime_checkable
from pathlib import Path

from reachy_mini_conversation_app.music.types import TrackInfo


logger = logging.getLogger(__name__)

LOCAL_AUDIO_EXTENSIONS: tuple[str, ...] = (".mp3", ".m4a", ".ogg", ".flac", ".wav", ".opus")
DEFAULT_SEARCH_LIMIT = 5


class MusicBackendError(RuntimeError):
    """Raised when a music backend cannot fulfil a request."""


@runtime_checkable
class MusicBackend(Protocol):
    """Async protocol for music sources (Yandex, local files, ...)."""

    name: str

    async def search(self, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> list[TrackInfo]:
        """Return up to `limit` candidate tracks matching `query`."""
        ...

    async def resolve_stream_url(self, track: TrackInfo) -> str:
        """Return a playable URL (or path) for the given track."""
        ...

    async def personal_station(self) -> list[TrackInfo]:
        """Return a curated queue (radio, personal mix, top tracks)."""
        ...

    async def kids_station(self) -> list[TrackInfo]:
        """Return a curated queue suitable for children."""
        ...


class LocalBackend:
    """Plays audio files stored on the robot's filesystem.

    Acts as the always-available fallback when no network backend is configured.
    """

    name = "local"

    def __init__(self, root: Path, kids_subdir: str = "kids") -> None:
        """Initialise the backend rooted at the directory holding audio files."""
        self._root = root
        self._kids_subdir = kids_subdir

    def _iter_files(self, scope: Path) -> list[Path]:
        if not scope.is_dir():
            return []
        return sorted(p for p in scope.rglob("*") if p.is_file() and p.suffix.lower() in LOCAL_AUDIO_EXTENSIONS)

    def _to_track(self, path: Path) -> TrackInfo:
        return TrackInfo(
            track_id=str(path.resolve()),
            title=path.stem,
            artist=path.parent.name if path.parent != self._root else "Local",
            duration_s=None,
            stream_url=str(path.resolve()),
            source=self.name,
        )

    async def search(self, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> list[TrackInfo]:
        """Substring match on filename and parent folder."""
        needle = query.strip().lower()
        if not needle:
            return [self._to_track(p) for p in self._iter_files(self._root)[:limit]]
        hits = [
            self._to_track(p)
            for p in self._iter_files(self._root)
            if needle in p.stem.lower() or needle in p.parent.name.lower()
        ]
        return hits[:limit]

    async def resolve_stream_url(self, track: TrackInfo) -> str:
        """Local files keep their URL on the TrackInfo itself."""
        if not track.stream_url:
            raise MusicBackendError(f"Local track has no path: {track.track_id}")
        return track.stream_url

    async def personal_station(self) -> list[TrackInfo]:
        """Return every track in root (capped)."""
        return [self._to_track(p) for p in self._iter_files(self._root)[: DEFAULT_SEARCH_LIMIT * 2]]

    async def kids_station(self) -> list[TrackInfo]:
        """Return tracks under the kids subdirectory."""
        kids_root = self._root / self._kids_subdir
        return [self._to_track(p) for p in self._iter_files(kids_root)[: DEFAULT_SEARCH_LIMIT * 2]]


class YandexBackend:
    """Yandex Music backend (unofficial API via the yandex-music package).

    Requires a Yandex OAuth token in YANDEX_MUSIC_TOKEN and an active Plus subscription.
    """

    name = "yandex"

    def __init__(self, token: str, kids_station_id: str = "genre:forchildren") -> None:
        """Store the OAuth token; lazy-initialise the client to avoid import cost."""
        try:
            from yandex_music import ClientAsync
        except ImportError as exc:
            raise MusicBackendError(
                "yandex-music package not installed. Install with: pip install yandex-music"
            ) from exc
        self._client_cls = ClientAsync
        self._token = token
        self._kids_station_id = kids_station_id
        self._client = None  # type: ignore[var-annotated]

    async def _get_client(self):  # type: ignore[no-untyped-def]
        if self._client is None:
            client = self._client_cls(self._token)
            await client.init()
            self._client = client
        return self._client

    def _track_to_info(self, track) -> TrackInfo:  # type: ignore[no-untyped-def]
        artists = ", ".join(a.name for a in getattr(track, "artists", []) if getattr(a, "name", None))
        duration_ms = getattr(track, "duration_ms", None)
        return TrackInfo(
            track_id=str(track.id),
            title=track.title or "",
            artist=artists or "Unknown",
            duration_s=(duration_ms / 1000.0) if duration_ms else None,
            stream_url=None,
            source=self.name,
        )

    async def search(self, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> list[TrackInfo]:
        """Search Yandex Music tracks by free-text query."""
        client = await self._get_client()
        result = await client.search(query, type_="track")
        tracks = getattr(getattr(result, "tracks", None), "results", []) or []
        return [self._track_to_info(t) for t in tracks[:limit]]

    async def resolve_stream_url(self, track: TrackInfo) -> str:
        """Resolve a signed download URL for the highest-bitrate available codec."""
        client = await self._get_client()
        download_infos = await client.tracks_download_info(track.track_id, get_direct_links=True)
        if not download_infos:
            raise MusicBackendError(f"No download info for track {track.track_id}")
        best = max(download_infos, key=lambda d: getattr(d, "bitrate_in_kbps", 0))
        url = getattr(best, "direct_link", None)
        if not url:
            raise MusicBackendError(f"Direct link missing for track {track.track_id}")
        return url

    async def personal_station(self) -> list[TrackInfo]:
        """Return tracks from the user's 'My Wave' personal station."""
        client = await self._get_client()
        station_tracks = await client.rotor_station_tracks("user:onyourwave")
        sequence = getattr(station_tracks, "sequence", []) or []
        return [self._track_to_info(item.track) for item in sequence if getattr(item, "track", None)]

    async def kids_station(self) -> list[TrackInfo]:
        """Return tracks from Yandex's kids music rotor station."""
        client = await self._get_client()
        station_tracks = await client.rotor_station_tracks(self._kids_station_id)
        sequence = getattr(station_tracks, "sequence", []) or []
        return [self._track_to_info(item.track) for item in sequence if getattr(item, "track", None)]
