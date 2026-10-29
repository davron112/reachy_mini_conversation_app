"""Tests for the dance tool (clamping and validation)."""

from unittest.mock import MagicMock

import pytest

from reachy_mini_conversation_app.tools.dance import (
    DANCE_SLOW_BPM,
    AVAILABLE_MOVES,
    DANCE_SPEED_BPM,
    DANCE_REPEAT_MAX,
    Dance,
)
from reachy_mini_conversation_app.tools.core_tools import ToolDependencies


def _deps_with_queue() -> tuple[ToolDependencies, MagicMock]:
    movement_manager = MagicMock()
    deps = ToolDependencies(reachy_mini=MagicMock(), movement_manager=movement_manager)
    return deps, movement_manager


@pytest.mark.asyncio
@pytest.mark.skipif(not AVAILABLE_MOVES, reason="dance library not installed")
async def test_dance_repeat_caps_at_max() -> None:
    """A huge repeat from the LLM is clamped to DANCE_REPEAT_MAX."""
    deps, movement_manager = _deps_with_queue()
    move_name = next(iter(AVAILABLE_MOVES))
    result = await Dance()(deps, move=move_name, repeat=10_000)
    assert result["status"] == "queued"
    assert result["repeat"] == DANCE_REPEAT_MAX
    assert movement_manager.queue_move.call_count == DANCE_REPEAT_MAX


@pytest.mark.asyncio
@pytest.mark.skipif(not AVAILABLE_MOVES, reason="dance library not installed")
async def test_dance_repeat_floors_at_min() -> None:
    """Zero or negative repeat is bumped up to DANCE_REPEAT_MIN."""
    deps, movement_manager = _deps_with_queue()
    move_name = next(iter(AVAILABLE_MOVES))
    result = await Dance()(deps, move=move_name, repeat=0)
    assert result["repeat"] == 1
    assert movement_manager.queue_move.call_count == 1


@pytest.mark.asyncio
@pytest.mark.skipif(not AVAILABLE_MOVES, reason="dance library not installed")
async def test_dance_repeat_invalid_falls_back_to_default() -> None:
    """Non-integer repeat values fall back to the default."""
    deps, movement_manager = _deps_with_queue()
    move_name = next(iter(AVAILABLE_MOVES))
    result = await Dance()(deps, move=move_name, repeat="not-a-number")
    assert result["repeat"] == 1
    assert movement_manager.queue_move.call_count == 1


def _queued_moves(movement_manager: MagicMock) -> list:
    return [call.args[0] for call in movement_manager.queue_move.call_args_list]


@pytest.mark.asyncio
@pytest.mark.skipif(not AVAILABLE_MOVES, reason="dance library not installed")
async def test_dance_slow_lowers_tempo_and_lengthens_move() -> None:
    """A slow dance plays the move at the slow tempo, so it lasts longer."""
    deps, movement_manager = _deps_with_queue()
    move_name = next(iter(AVAILABLE_MOVES))
    await Dance()(deps, move=move_name)
    normal = _queued_moves(movement_manager)[0]
    deps, movement_manager = _deps_with_queue()
    result = await Dance()(deps, move=move_name, speed="slow")
    slow = _queued_moves(movement_manager)[0]
    assert result["speed"] == "slow"
    assert slow.dance_move.default_bpm == DANCE_SLOW_BPM
    assert slow.duration > normal.duration


@pytest.mark.asyncio
@pytest.mark.skipif(not AVAILABLE_MOVES, reason="dance library not installed")
async def test_dance_defaults_to_normal_speed() -> None:
    """Without a speed the library tempo is kept."""
    deps, movement_manager = _deps_with_queue()
    result = await Dance()(deps, move=next(iter(AVAILABLE_MOVES)))
    assert result["speed"] == "normal"
    assert _queued_moves(movement_manager)[0].dance_move.default_bpm == DANCE_SPEED_BPM["normal"]


@pytest.mark.asyncio
@pytest.mark.skipif(not AVAILABLE_MOVES, reason="dance library not installed")
async def test_dance_rejects_unknown_speed() -> None:
    """An unknown speed is reported back instead of queueing anything."""
    deps, movement_manager = _deps_with_queue()
    result = await Dance()(deps, move=next(iter(AVAILABLE_MOVES)), speed="warp")
    assert "error" in result
    movement_manager.queue_move.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.skipif(not AVAILABLE_MOVES, reason="dance library not installed")
async def test_new_dance_replaces_the_one_in_progress() -> None:
    """A new dance request clears the queue before queueing, so a speed change takes effect now."""
    deps, movement_manager = _deps_with_queue()
    await Dance()(deps, move=next(iter(AVAILABLE_MOVES)), repeat=2, speed="slow")
    names = [call[0] for call in movement_manager.method_calls]
    assert names == ["clear_move_queue", "queue_move", "queue_move"]


def test_dance_schema_offers_speed() -> None:
    """The LLM sees the speed choices."""
    assert Dance.parameters_schema["properties"]["speed"]["enum"] == list(DANCE_SPEED_BPM)
