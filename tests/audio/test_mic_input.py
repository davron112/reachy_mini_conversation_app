"""Tests for shared microphone frame preparation."""

import numpy as np
import pytest

from reachy_mini_conversation_app.audio.mic_input import (
    INT16_MAX,
    INT16_MIN,
    MIC_GAIN_ENV,
    DEFAULT_MIC_GAIN,
    MIC_CHANNEL_MIX_ENV,
    downmix,
    mic_gain,
    prepare_mic_frame,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(MIC_GAIN_ENV, raising=False)
    monkeypatch.delenv(MIC_CHANNEL_MIX_ENV, raising=False)


@pytest.mark.parametrize("raw", ["", "abc", "0", "-2"])
def test_mic_gain_falls_back_to_unity(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """Invalid or non-positive gain values fall back to unity."""
    monkeypatch.setenv(MIC_GAIN_ENV, raw)
    assert mic_gain() == DEFAULT_MIC_GAIN


def test_mic_gain_unset_is_unity() -> None:
    """An unset gain leaves the signal unchanged."""
    assert mic_gain() == DEFAULT_MIC_GAIN


def test_mic_gain_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid gain is read from the environment."""
    monkeypatch.setenv(MIC_GAIN_ENV, "2.5")
    assert mic_gain() == 2.5


def test_downmix_absmax_keeps_louder_signed_sample() -> None:
    """Absmax keeps the louder channel per sample, sign included."""
    stereo = np.array([[100.0, -300.0], [-50.0, 20.0]])
    np.testing.assert_array_equal(downmix(stereo, "absmax"), [-300.0, -50.0])


def test_downmix_accepts_channels_first() -> None:
    """Channel-first frames are transposed before mixing."""
    stereo = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
    np.testing.assert_array_equal(downmix(stereo, "first"), [1.0, 2.0, 3.0])
    np.testing.assert_array_equal(downmix(stereo, "second"), [4.0, 5.0, 6.0])
    np.testing.assert_array_equal(downmix(stereo, "mean"), [2.5, 3.5, 4.5])


def test_downmix_single_channel_column() -> None:
    """A single-channel column collapses to mono."""
    np.testing.assert_array_equal(downmix(np.array([[7.0], [8.0]]), "absmax"), [7.0, 8.0])


@pytest.mark.parametrize("dtype", [np.int16, np.int32])
def test_unity_gain_returns_int16_for_integer_dtypes(dtype: type) -> None:
    """Integer frames come out as int16 even without gain."""
    frame = np.array([[1000, -2000], [300, 400]], dtype=dtype)
    out = prepare_mic_frame(frame, 16000, 16000)
    assert out.dtype == np.int16
    np.testing.assert_array_equal(out, [-2000, 400])


@pytest.mark.parametrize("dtype", [np.float32, np.float64])
def test_float_frames_are_treated_as_normalized(dtype: type) -> None:
    """Float frames are scaled from [-1, 1] to int16."""
    out = prepare_mic_frame(np.array([0.5, -1.0], dtype=dtype), 16000, 16000)
    np.testing.assert_array_equal(out, [16384, -32767])


def test_gain_amplifies_and_clips(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gain amplifies and saturates instead of wrapping."""
    monkeypatch.setenv(MIC_GAIN_ENV, "3.0")
    out = prepare_mic_frame(np.array([1000, 20000, -20000], dtype=np.int16), 16000, 16000)
    np.testing.assert_array_equal(out, [3000, INT16_MAX, INT16_MIN])


def test_resamples_to_target_rate() -> None:
    """Frames are resampled to the backend rate."""
    frame = np.zeros(480, dtype=np.int16)
    out = prepare_mic_frame(frame, 16000, 24000)
    assert out.shape == (720,)
    assert out.dtype == np.int16


def test_empty_frame() -> None:
    """An empty frame stays empty and int16."""
    out = prepare_mic_frame(np.zeros(0, dtype=np.int16), 16000, 24000)
    assert out.dtype == np.int16
    assert out.size == 0
