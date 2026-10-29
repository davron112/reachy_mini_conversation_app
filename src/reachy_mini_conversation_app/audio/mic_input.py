"""Microphone frame preparation shared by every realtime backend."""

import os
import logging

import numpy as np
from numpy.typing import NDArray


logger = logging.getLogger(__name__)

MIC_GAIN_ENV = "REACHY_MIC_GAIN"
MIC_CHANNEL_MIX_ENV = "REACHY_MIC_CHANNEL_MIX"
DEFAULT_MIC_GAIN = 1.0
CHANNEL_MIX_ABSMAX = "absmax"
CHANNEL_MIX_MEAN = "mean"
CHANNEL_MIX_FIRST = "first"
CHANNEL_MIX_SECOND = "second"
INT16_FULL_SCALE = 32767.0
INT16_MIN = -32768
INT16_MAX = 32767


def mic_gain() -> float:
    """Return the configured software mic gain, falling back to unity on bad input."""
    raw = os.getenv(MIC_GAIN_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_MIC_GAIN
    try:
        gain = float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, using %s", MIC_GAIN_ENV, raw, DEFAULT_MIC_GAIN)
        return DEFAULT_MIC_GAIN
    if gain <= 0:
        logger.warning("Non-positive %s=%r, using %s", MIC_GAIN_ENV, raw, DEFAULT_MIC_GAIN)
        return DEFAULT_MIC_GAIN
    return gain


def channel_mix() -> str:
    """Return the configured stereo downmix strategy."""
    return (os.getenv(MIC_CHANNEL_MIX_ENV) or CHANNEL_MIX_ABSMAX).strip().lower()


def _to_int16_scale(audio: NDArray[np.generic]) -> NDArray[np.float64]:
    if np.issubdtype(audio.dtype, np.floating):
        return audio.astype(np.float64) * INT16_FULL_SCALE
    return audio.astype(np.float64)


def _channels_last(audio: NDArray[np.float64]) -> NDArray[np.float64]:
    if audio.shape[1] > audio.shape[0]:
        return audio.T
    return audio


def downmix(audio: NDArray[np.float64], mix: str) -> NDArray[np.float64]:
    """Collapse a (samples, channels) or (channels, samples) frame to mono.

    The default absmax keeps the louder channel per sample, so phase-shifted
    channels of the mic array never cancel each other out.
    """
    if audio.ndim == 1:
        return audio
    audio = _channels_last(audio)
    if audio.shape[1] == 1:
        return audio[:, 0]
    if mix == CHANNEL_MIX_MEAN:
        return np.asarray(audio.mean(axis=1), dtype=np.float64)
    if mix == CHANNEL_MIX_FIRST:
        return audio[:, 0]
    if mix == CHANNEL_MIX_SECOND:
        return audio[:, 1]
    loudest = np.abs(audio).argmax(axis=1)
    return np.asarray(audio[np.arange(audio.shape[0]), loudest], dtype=np.float64)


def resample_linear(audio: NDArray[np.float64], input_rate: int, target_rate: int) -> NDArray[np.float64]:
    """Resample a mono frame by linear interpolation."""
    target_len = int(len(audio) * target_rate / input_rate)
    if target_len <= 0:
        return audio[:0]
    source_positions = np.arange(len(audio), dtype=np.float64)
    target_positions = np.linspace(0.0, len(audio) - 1, target_len)
    return np.interp(target_positions, source_positions, audio)


def prepare_mic_frame(audio: NDArray[np.generic], input_rate: int, target_rate: int) -> NDArray[np.int16]:
    """Downmix, resample, apply gain and convert a mic frame to int16 mono PCM."""
    mono = downmix(_to_int16_scale(audio), channel_mix())
    if input_rate != target_rate and len(mono):
        mono = resample_linear(mono, input_rate, target_rate)
    gain = mic_gain()
    if gain != DEFAULT_MIC_GAIN:
        mono = mono * gain
    return np.clip(np.rint(mono), INT16_MIN, INT16_MAX).astype(np.int16)
