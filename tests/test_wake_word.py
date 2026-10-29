"""Unit tests for the wake-word detector and backends.

These tests avoid loading any real ML model — backends are constructed
directly and the queue/thread machinery is exercised with fakes.
"""

from __future__ import annotations
import json
import asyncio
from typing import Any

import numpy as np
import pytest

from reachy_mini_conversation_app.wake_word.detector import (
    ENV_ENGINE,
    ENV_ENABLED,
    WakeWordBackend,
    WakeWordDetector,
    OpenWakeWordBackend,
    _float_env,
    build_wake_word_detector,
    _build_openwakeword_backend,
)
from reachy_mini_conversation_app.wake_word.vosk_backend import (
    FINAL_SCORE,
    PARTIAL_SCORE,
    ENV_VOSK_MODEL,
    ENV_VOSK_PHRASES,
    VoskBackend,
    build_vosk_backend,
)


class _FakeBackend(WakeWordBackend):
    """Backend that fires on any non-empty frame; useful for thread tests."""

    def __init__(self, label: str = "test", score: float = 0.99) -> None:
        self._label = label
        self._score = score
        self.load_called = False
        self.closed = False

    @property
    def label(self) -> str:
        return self._label

    def load(self) -> None:
        self.load_called = True

    def process(self, frame_int16: np.ndarray) -> list[tuple[str, float]]:
        if frame_int16.size == 0:
            return []
        return [(self._label, self._score)]

    def close(self) -> None:
        self.closed = True


def test_float_env_returns_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset env var falls back to the provided default."""
    monkeypatch.delenv("REACHY_TEST_FLOAT", raising=False)
    assert _float_env("REACHY_TEST_FLOAT", 7.5) == 7.5


def test_float_env_handles_invalid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-numeric env value is treated as missing, default is returned."""
    monkeypatch.setenv("REACHY_TEST_FLOAT", "not-a-float")
    assert _float_env("REACHY_TEST_FLOAT", 3.0) == 3.0


def test_build_returns_none_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Factory returns None when the master enable flag is unset."""
    monkeypatch.delenv(ENV_ENABLED, raising=False)
    loop = asyncio.new_event_loop()
    try:
        assert build_wake_word_detector(on_wake=_noop_callback, loop=loop) is None
    finally:
        loop.close()


def test_build_returns_none_for_unknown_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown engine value is rejected by the factory."""
    monkeypatch.setenv(ENV_ENABLED, "1")
    monkeypatch.setenv(ENV_ENGINE, "telepathy")
    loop = asyncio.new_event_loop()
    try:
        assert build_wake_word_detector(on_wake=_noop_callback, loop=loop) is None
    finally:
        loop.close()


def test_build_openwakeword_returns_none_when_model_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing openWakeWord model file disables the backend cleanly."""
    monkeypatch.setenv("REACHY_WAKE_WORD_MODEL", "/definitely/not/a/real/model.onnx")
    assert _build_openwakeword_backend() is None


def test_vosk_backend_requires_phrases() -> None:
    """Constructor rejects an empty phrase list."""
    with pytest.raises(ValueError):
        VoskBackend(model_path="/tmp/whatever", phrases=[])


def test_vosk_backend_label_is_first_phrase() -> None:
    """The label reported externally is the first configured phrase."""
    backend = VoskBackend(model_path="/tmp/whatever", phrases=["hey guli", "guli"])
    assert backend.label == "hey guli"


def test_vosk_backend_match_returns_full_score_for_finalized() -> None:
    """Finalized transcripts containing the phrase produce the full-confidence score."""
    backend = VoskBackend(model_path="/tmp/whatever", phrases=["hey guli", "guli"])
    assert backend._match("hey guli wie geht es dir", score=FINAL_SCORE) == ("hey guli", FINAL_SCORE)


def test_vosk_backend_match_returns_partial_score_for_partial() -> None:
    """Partial transcripts produce the partial score (a phrase still matches)."""
    backend = VoskBackend(model_path="/tmp/whatever", phrases=["hey guli", "guli"])
    assert backend._match("guli", score=PARTIAL_SCORE) == ("hey guli", PARTIAL_SCORE) or \
        backend._match("guli", score=PARTIAL_SCORE) == ("guli", PARTIAL_SCORE)


def test_vosk_backend_match_returns_none_for_unrelated_text() -> None:
    """Text that doesn't contain any phrase yields no match."""
    backend = VoskBackend(model_path="/tmp/whatever", phrases=["hey guli"])
    assert backend._match("salom dunyo", score=FINAL_SCORE) is None


def test_vosk_backend_match_ignores_empty_text() -> None:
    """Empty input yields no match (and avoids spurious fires)."""
    backend = VoskBackend(model_path="/tmp/whatever", phrases=["hey guli"])
    assert backend._match("", score=FINAL_SCORE) is None


def test_vosk_backend_extract_text() -> None:
    """JSON ``text`` field is normalized to lowercase trimmed string."""
    backend = VoskBackend(model_path="/tmp/whatever", phrases=["hey guli"])
    raw = json.dumps({"text": "Hey Guli  "})
    assert backend._extract(raw, "text") == "hey guli"


def test_vosk_backend_extract_handles_invalid_json() -> None:
    """Malformed JSON from the recognizer yields an empty string, not an exception."""
    backend = VoskBackend(model_path="/tmp/whatever", phrases=["hey guli"])
    assert backend._extract("not json", "text") == ""


def test_vosk_backend_process_with_fake_recognizer() -> None:
    """process() should consult the recognizer and surface matches."""
    backend = VoskBackend(model_path="/tmp/whatever", phrases=["hey guli", "guli"])

    class _FakeRecognizer:
        def __init__(self) -> None:
            self.calls = 0

        def AcceptWaveform(self, _pcm: bytes) -> bool:
            self.calls += 1
            return self.calls % 2 == 0

        def PartialResult(self) -> str:
            return json.dumps({"partial": "guli"})

        def Result(self) -> str:
            return json.dumps({"text": "hey guli"})

    backend._recognizer = _FakeRecognizer()  # type: ignore[assignment]
    frame = np.zeros(1024, dtype=np.int16)
    partial = backend.process(frame)
    assert partial == [("guli", PARTIAL_SCORE)] or partial == [("hey guli", PARTIAL_SCORE)]
    final = backend.process(frame)
    assert final == [("hey guli", FINAL_SCORE)]


def test_build_vosk_backend_requires_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the Vosk model env var the factory returns None."""
    monkeypatch.delenv(ENV_VOSK_MODEL, raising=False)
    assert build_vosk_backend() is None


def test_build_vosk_backend_parses_phrases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Comma-separated phrase list is split and trimmed."""
    monkeypatch.setenv(ENV_VOSK_MODEL, "/tmp/whatever")
    monkeypatch.setenv(ENV_VOSK_PHRASES, "hey guli, guli,  ey guli  ")
    backend = build_vosk_backend()
    assert backend is not None
    assert backend._phrases == ["hey guli", "guli", "ey guli"]


def test_build_vosk_backend_uses_defaults_when_phrases_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default phrase list is used when the env var is empty."""
    monkeypatch.setenv(ENV_VOSK_MODEL, "/tmp/whatever")
    monkeypatch.delenv(ENV_VOSK_PHRASES, raising=False)
    backend = build_vosk_backend()
    assert backend is not None
    assert "hey guli" in backend._phrases


def test_open_wakeword_backend_label_from_filename() -> None:
    """OpenWakeWord backend derives its label from the model filename stem."""
    backend = OpenWakeWordBackend(model_path="/tmp/hey_guli_v0.1.onnx", threshold=0.5)
    assert backend.label == "hey_guli_v0.1"


@pytest.mark.asyncio
async def test_detector_fires_callback_with_cooldown() -> None:
    """The detector should fire on a detection, then suppress further fires until cooldown elapses."""
    loop = asyncio.get_running_loop()
    fired: list[tuple[str, float]] = []

    async def on_wake(label: str, score: float) -> None:
        fired.append((label, score))

    backend = _FakeBackend(label="guli", score=0.95)
    detector = WakeWordDetector(backend=backend, on_wake=on_wake, loop=loop, cooldown_s=0.2)
    detector.start()
    try:
        for _ in range(5):
            detector.feed(np.ones(1024, dtype=np.int16))
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        first_round = len(fired)
        assert first_round >= 1
        assert first_round <= 2  # cooldown should suppress rapid repeats

        await asyncio.sleep(0.25)
        for _ in range(3):
            detector.feed(np.ones(1024, dtype=np.int16))
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        assert len(fired) > first_round  # next firing after cooldown
    finally:
        detector.stop()
        assert backend.closed is True


@pytest.mark.asyncio
async def test_detector_skips_when_backend_load_fails(caplog: pytest.LogCaptureFixture) -> None:
    """If the backend's load() raises, the worker thread exits cleanly without firing."""
    loop = asyncio.get_running_loop()

    class _BrokenBackend(_FakeBackend):
        def load(self) -> None:
            raise RuntimeError("boom")

    fired: list[Any] = []

    async def on_wake(label: str, score: float) -> None:
        fired.append((label, score))

    detector = WakeWordDetector(backend=_BrokenBackend(), on_wake=on_wake, loop=loop, cooldown_s=0.1)
    detector.start()
    try:
        detector.feed(np.ones(1024, dtype=np.int16))
        await asyncio.sleep(0.1)
        assert fired == []
    finally:
        detector.stop()


async def _noop_callback(_label: str, _score: float) -> None:
    return None
