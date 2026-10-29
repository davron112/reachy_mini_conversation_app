"""Tests for the half-duplex playback echo gate."""

import pytest

from reachy_mini_conversation_app.audio.echo_gate import (
    ECHO_TAIL_ENV,
    HALF_DUPLEX_ENV,
    DEFAULT_ECHO_TAIL_S,
    PlaybackEchoGate,
    build_echo_gate,
)


class FakeClock:
    """Manually advanced monotonic clock."""

    def __init__(self) -> None:
        """Start at an arbitrary non-zero time."""
        self.now = 100.0

    def __call__(self) -> float:
        """Return the current fake time."""
        return self.now


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(HALF_DUPLEX_ENV, raising=False)
    monkeypatch.delenv(ECHO_TAIL_ENV, raising=False)


def test_gate_open_without_playback() -> None:
    """A fresh gate lets mic frames through."""
    assert PlaybackEchoGate(0.5, FakeClock()).is_open


def test_gate_closed_during_playback_and_tail() -> None:
    """The gate stays closed for the queued audio plus the echo tail."""
    clock = FakeClock()
    gate = PlaybackEchoGate(0.5, clock)
    gate.note_playback(1.0)
    clock.now += 1.2
    assert not gate.is_open
    clock.now += 0.31
    assert gate.is_open


def test_queued_frames_accumulate() -> None:
    """Frames pushed faster than real time extend the playback end."""
    clock = FakeClock()
    gate = PlaybackEchoGate(0.0, clock)
    for _ in range(10):
        gate.note_playback(0.1)
    clock.now += 0.9
    assert not gate.is_open
    clock.now += 0.11
    assert gate.is_open


def test_playback_after_silence_starts_from_now() -> None:
    """A new reply after a pause is timed from the current moment."""
    clock = FakeClock()
    gate = PlaybackEchoGate(0.0, clock)
    gate.note_playback(1.0)
    clock.now += 10.0
    gate.note_playback(0.5)
    clock.now += 0.4
    assert not gate.is_open
    clock.now += 0.11
    assert gate.is_open


def test_reset_reopens_immediately() -> None:
    """Flushing the player queue reopens the mic at once."""
    clock = FakeClock()
    gate = PlaybackEchoGate(0.5, clock)
    gate.note_playback(5.0)
    gate.reset()
    assert gate.is_open


def test_build_defaults_to_enabled_with_default_tail() -> None:
    """Half-duplex is on by default."""
    gate = build_echo_gate()
    assert gate is not None
    assert gate._tail_s == DEFAULT_ECHO_TAIL_S


@pytest.mark.parametrize("value", ["0", "false", "off"])
def test_build_can_be_disabled(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Half-duplex can be turned off for full-duplex setups with hardware AEC."""
    monkeypatch.setenv(HALF_DUPLEX_ENV, value)
    assert build_echo_gate() is None


@pytest.mark.parametrize(("raw", "expected"), [("0.8", 0.8), ("bad", DEFAULT_ECHO_TAIL_S), ("-1", 0.0)])
def test_build_reads_tail(monkeypatch: pytest.MonkeyPatch, raw: str, expected: float) -> None:
    """The tail is configurable and sanitised."""
    monkeypatch.setenv(ECHO_TAIL_ENV, raw)
    gate = build_echo_gate()
    assert gate is not None
    assert gate._tail_s == expected
