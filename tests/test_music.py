"""Tests for the music subsystem (backend selection, session orchestration)."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from reachy_mini_conversation_app.music.types import TrackInfo
from reachy_mini_conversation_app.music.session import (
    MAX_VOLUME,
    MIN_VOLUME,
    MusicSession,
    MusicSessionError,
)


def _track(track_id: str = "1", title: str = "Song", artist: str = "Band") -> TrackInfo:
    return TrackInfo(
        track_id=track_id,
        title=title,
        artist=artist,
        duration_s=180.0,
        stream_url="https://stream/x",
        source="fake",
    )


def _fake_backend(name: str = "fake", **overrides: Any) -> MagicMock:
    backend = MagicMock()
    backend.name = name
    backend.search = AsyncMock(return_value=overrides.get("search", [_track()]))
    backend.resolve_stream_url = AsyncMock(return_value="https://stream/x")
    backend.personal_station = AsyncMock(return_value=overrides.get("personal", [_track()]))
    backend.kids_station = AsyncMock(return_value=overrides.get("kids", [_track()]))
    return backend


def _fake_player() -> MagicMock:
    player = MagicMock()
    player.name = "fake-player"
    player.play = AsyncMock()
    player.pause = AsyncMock()
    player.resume = AsyncMock()
    player.stop = AsyncMock()
    player.set_volume = AsyncMock()
    player.get_volume = AsyncMock(return_value=70)
    player.is_playing = AsyncMock(return_value=True)
    player.close = AsyncMock()
    return player


@pytest.mark.asyncio
async def test_play_query_plays_top_and_queues_remainder() -> None:
    """Playing a query starts the first hit and queues the rest."""
    backend = _fake_backend(search=[_track("1"), _track("2"), _track("3")])
    player = _fake_player()
    session = MusicSession(backend=backend, player=player)

    track = await session.play_query("imagine dragons")

    assert track.track_id == "1"
    assert [t.track_id for t in session.queue] == ["2", "3"]
    player.play.assert_awaited_once()


@pytest.mark.asyncio
async def test_play_query_raises_when_no_results() -> None:
    """A query with no search results raises."""
    backend = _fake_backend(search=[])
    session = MusicSession(backend=backend, player=_fake_player())
    with pytest.raises(MusicSessionError):
        await session.play_query("doesntexist")


@pytest.mark.asyncio
async def test_next_track_advances_and_then_stops() -> None:
    """Next advances through the queue and stops at its end."""
    backend = _fake_backend(search=[_track("1"), _track("2")])
    player = _fake_player()
    session = MusicSession(backend=backend, player=player)

    await session.play_query("anything")
    nxt = await session.next_track()
    assert nxt is not None and nxt.track_id == "2"

    done = await session.next_track()
    assert done is None
    player.stop.assert_awaited()


@pytest.mark.asyncio
async def test_set_volume_clamps_into_range() -> None:
    """Volume requests are clamped into the allowed range."""
    session = MusicSession(backend=_fake_backend(), player=_fake_player())
    assert await session.set_volume(500) == MAX_VOLUME
    assert await session.set_volume(-10) == MIN_VOLUME


@pytest.mark.asyncio
async def test_duck_and_unduck_round_trip() -> None:
    """Ducking lowers the volume and unducking restores it."""
    backend = _fake_backend()
    player = _fake_player()
    player.get_volume = AsyncMock(return_value=80)
    session = MusicSession(backend=backend, player=player, duck_volume=25)

    await session.play_query("anything")
    await session.duck()
    player.set_volume.assert_awaited_with(25)

    await session.unduck()
    player.set_volume.assert_awaited_with(80)


@pytest.mark.asyncio
async def test_duck_noop_when_nothing_playing() -> None:
    """Ducking with nothing playing is a no-op."""
    session = MusicSession(backend=_fake_backend(), player=_fake_player())
    await session.duck()
    session._player.set_volume.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_kids_station_starts_from_top() -> None:
    """The kids station starts from its first track."""
    backend = _fake_backend(kids=[_track("k1"), _track("k2")])
    session = MusicSession(backend=backend, player=_fake_player())
    track = await session.play_kids_station()
    assert track.track_id == "k1"
    assert [t.track_id for t in session.queue] == ["k2"]
