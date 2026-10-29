"""Vosk-based wake-word backend: grammar-restricted streaming KaldiRecognizer.

Vosk is given a tiny grammar of just the configured wake phrases plus the
unknown token, so the decoder rejects everything else. Cheap, accurate,
trained on diverse voices (handles child speech and accented English).
"""

from __future__ import annotations
import os
import json
import logging
from typing import Optional
from pathlib import Path

import numpy as np

from reachy_mini_conversation_app.wake_word.detector import WakeWordBackend


logger = logging.getLogger(__name__)


ENV_VOSK_MODEL = "REACHY_WAKE_WORD_VOSK_MODEL"
ENV_VOSK_PHRASES = "REACHY_WAKE_WORD_PHRASES"
ENV_VOSK_LOG_LEVEL = "REACHY_WAKE_WORD_VOSK_LOG_LEVEL"

DEFAULT_PHRASES = ("hey guli", "guli")
DEFAULT_LOG_LEVEL = -1

VOSK_SAMPLE_RATE = 16000
PARTIAL_SCORE = 0.9
FINAL_SCORE = 1.0


class VoskBackend(WakeWordBackend):
    """Grammar-restricted Vosk KaldiRecognizer used as a wake-phrase spotter."""

    def __init__(self, model_path: str, phrases: list[str]) -> None:
        """Store config; the Vosk model is loaded lazily in ``load()``."""
        cleaned = [p.strip().lower() for p in phrases if p and p.strip()]
        if not cleaned:
            raise ValueError("VoskBackend requires at least one wake phrase")
        self._model_path = model_path
        self._phrases = cleaned
        self._label_value = cleaned[0]
        self._recognizer: object | None = None
        self._last_partial = ""

    @property
    def label(self) -> str:
        """Primary wake-phrase reported on detection (the first configured phrase)."""
        return self._label_value

    def load(self) -> None:
        """Import vosk and build a grammar-restricted KaldiRecognizer."""
        try:
            import vosk
        except ImportError as exc:
            raise RuntimeError("vosk not installed: pip install vosk") from exc

        try:
            vosk.SetLogLevel(int(os.getenv(ENV_VOSK_LOG_LEVEL, str(DEFAULT_LOG_LEVEL))))
        except (ValueError, AttributeError):
            pass

        resolved = Path(self._model_path).expanduser()
        if not resolved.exists():
            raise FileNotFoundError(f"Vosk model not found: {resolved}")

        model = vosk.Model(str(resolved))
        grammar = json.dumps(self._phrases + ["[unk]"], ensure_ascii=False)
        self._recognizer = vosk.KaldiRecognizer(model, VOSK_SAMPLE_RATE, grammar)
        logger.info(
            "Vosk wake-word backend loaded: model=%s phrases=%s",
            resolved, self._phrases,
        )

    def process(self, frame_int16: np.ndarray) -> list[tuple[str, float]]:
        """Feed PCM to the recognizer and surface any wake-phrase match."""
        if self._recognizer is None:
            return []

        pcm = frame_int16.astype(np.int16, copy=False).tobytes()

        try:
            finalized = self._recognizer.AcceptWaveform(pcm)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            logger.debug("Vosk AcceptWaveform failed: %s", exc)
            return []

        if finalized:
            raw = self._recognizer.Result()  # type: ignore[attr-defined]
            self._last_partial = ""
            match = self._match(self._extract(raw, "text"), score=FINAL_SCORE)
        else:
            raw = self._recognizer.PartialResult()  # type: ignore[attr-defined]
            text = self._extract(raw, "partial")
            if not text or text == self._last_partial:
                return []
            self._last_partial = text
            match = self._match(text, score=PARTIAL_SCORE)

        return [match] if match is not None else []

    def _extract(self, raw: str, key: str) -> str:
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return ""
        return (obj.get(key) or "").strip().lower()

    def _match(self, text: str, score: float) -> Optional[tuple[str, float]]:
        if not text:
            return None
        for phrase in self._phrases:
            if phrase and phrase in text:
                return (phrase, score)
        return None


def build_vosk_backend() -> Optional[VoskBackend]:
    """Construct a VoskBackend from env config, or None if model path missing."""
    model_path = (os.getenv(ENV_VOSK_MODEL) or "").strip()
    if not model_path:
        logger.warning("%s not set; Vosk wake-word backend disabled", ENV_VOSK_MODEL)
        return None

    phrases_raw = (os.getenv(ENV_VOSK_PHRASES) or "").strip()
    if phrases_raw:
        phrases = [p.strip() for p in phrases_raw.split(",") if p.strip()]
    else:
        phrases = list(DEFAULT_PHRASES)

    try:
        return VoskBackend(model_path=model_path, phrases=phrases)
    except ValueError as exc:
        logger.warning("Vosk backend config invalid: %s", exc)
        return None
