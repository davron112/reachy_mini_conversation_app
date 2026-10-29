"""Backend-agnostic wake-word detector with a thread-safe push API.

The detector owns the mic-frame queue, the worker thread, and the cooldown.
Inference is delegated to a `WakeWordBackend` (openWakeWord, Vosk, ...).
"""

from __future__ import annotations
import os
import time
import queue
import asyncio
import logging
import threading
from abc import ABC, abstractmethod
from typing import Callable, Optional, Awaitable
from pathlib import Path

import numpy as np


logger = logging.getLogger(__name__)


ENV_ENABLED = "REACHY_WAKE_WORD_ENABLED"
ENV_ENGINE = "REACHY_WAKE_WORD_ENGINE"
ENV_COOLDOWN = "REACHY_WAKE_WORD_COOLDOWN_S"

ENV_OWW_MODEL = "REACHY_WAKE_WORD_MODEL"
ENV_OWW_THRESHOLD = "REACHY_WAKE_WORD_THRESHOLD"
ENV_OWW_FRAMEWORK = "REACHY_WAKE_WORD_FRAMEWORK"

DEFAULT_ENGINE = "openwakeword"
DEFAULT_COOLDOWN_S = 2.0
DEFAULT_OWW_THRESHOLD = 0.5

OPENWAKEWORD_SAMPLE_RATE = 16000
OPENWAKEWORD_CHUNK_SAMPLES = 1280


WakeCallback = Callable[[str, float], Awaitable[None]]


def _is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r, using default %s", name, raw, default)
        return default


class WakeWordBackend(ABC):
    """Abstract wake-word inference backend.

    Implementations buffer mono int16 frames internally and emit detections
    as (label, score) tuples whenever the configured wake phrase fires above
    that backend's own threshold.
    """

    @property
    @abstractmethod
    def label(self) -> str:
        """Primary label this backend reports (used for logging)."""
        raise NotImplementedError

    @abstractmethod
    def load(self) -> None:
        """Load model weights / initialise inference. Raises on failure."""
        raise NotImplementedError

    @abstractmethod
    def process(self, frame_int16: np.ndarray) -> list[tuple[str, float]]:
        """Process one mono int16 audio frame; return any detections."""
        raise NotImplementedError

    def close(self) -> None:
        """Release resources. Default no-op."""
        return


class OpenWakeWordBackend(WakeWordBackend):
    """openWakeWord ONNX/TFLite inference (legacy default)."""

    def __init__(self, model_path: str, threshold: float, framework: str = "onnx") -> None:
        """Configure the backend; the model itself loads lazily in ``load()``."""
        self._model_path = model_path
        self._threshold = threshold
        self._framework = framework
        self._model: object | None = None
        self._buffer = np.empty(0, dtype=np.int16)
        self._label_value = Path(model_path).stem
        self._max_observed: dict[str, float] = {}
        self._frames_since_log = 0

    @property
    def label(self) -> str:
        """Wake-word label, derived from the model filename stem."""
        return self._label_value

    def load(self) -> None:
        """Import openwakeword and instantiate the inference model."""
        try:
            from openwakeword.model import Model
        except ImportError as exc:
            raise RuntimeError("openwakeword not installed") from exc

        self._model = Model(
            wakeword_model_paths=[self._model_path],
            inference_framework=self._framework,
        )
        logger.info("openWakeWord loaded; classes=%s", list(self._model.models.keys()))  # type: ignore[attr-defined]

    def process(self, frame_int16: np.ndarray) -> list[tuple[str, float]]:
        """Buffer the frame into 80 ms chunks, score, and return any detections."""
        if self._model is None:
            return []
        self._buffer = np.concatenate([self._buffer, frame_int16.astype(np.int16, copy=False)])
        detections: list[tuple[str, float]] = []
        while len(self._buffer) >= OPENWAKEWORD_CHUNK_SAMPLES:
            chunk = self._buffer[:OPENWAKEWORD_CHUNK_SAMPLES]
            self._buffer = self._buffer[OPENWAKEWORD_CHUNK_SAMPLES:]
            try:
                scores = self._model.predict(chunk)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                logger.debug("openWakeWord predict failed: %s", exc)
                continue
            for label, score in scores.items():
                if score > self._max_observed.get(label, 0.0):
                    self._max_observed[label] = float(score)
                if score >= self._threshold:
                    detections.append((label, float(score)))
            self._frames_since_log += 1
            if self._frames_since_log >= 250:
                logger.info(
                    "Wake-word max score (last 20s): %s",
                    {k: round(v, 3) for k, v in self._max_observed.items()},
                )
                self._max_observed.clear()
                self._frames_since_log = 0
        return detections


class WakeWordDetector:
    """Generic wake-word detector with a thread-safe push API.

    Push int16 mono mic frames into ``feed()``; the worker thread runs the
    configured backend and calls ``on_wake(label, score)`` on the provided
    asyncio loop whenever the wake phrase fires.
    """

    def __init__(
        self,
        backend: WakeWordBackend,
        on_wake: WakeCallback,
        loop: asyncio.AbstractEventLoop,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
    ) -> None:
        """Wire the detector to a backend, a callback, and an asyncio loop."""
        self._backend = backend
        self._on_wake = on_wake
        self._loop = loop
        self._cooldown_s = cooldown_s

        self._queue: "queue.Queue[Optional[np.ndarray]]" = queue.Queue(maxsize=128)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_fire_at = 0.0

    @property
    def label(self) -> str:
        """Wake-word label, forwarded from the underlying backend."""
        return self._backend.label

    def start(self) -> None:
        """Spawn the worker thread that runs backend inference."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wake-word", daemon=True)
        self._thread.start()
        logger.info(
            "WakeWordDetector started: backend=%s cooldown=%.2fs",
            type(self._backend).__name__, self._cooldown_s,
        )

    def stop(self) -> None:
        """Signal the worker to exit, join it, and release backend resources."""
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        try:
            self._backend.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Backend close raised: %s", exc)

    def feed(self, mono_int16: np.ndarray) -> None:
        """Push a mono int16 frame onto the inference queue (non-blocking)."""
        if self._stop.is_set():
            return
        try:
            self._queue.put_nowait(mono_int16)
        except queue.Full:
            pass

    def _run(self) -> None:
        try:
            self._backend.load()
        except Exception as exc:  # noqa: BLE001
            logger.error("Wake-word backend %s failed to load: %s", type(self._backend).__name__, exc)
            return

        while not self._stop.is_set():
            frame = self._queue.get()
            if frame is None:
                break
            try:
                detections = self._backend.process(frame)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Wake-word backend process failed: %s", exc)
                continue
            for label, score in detections:
                self._maybe_fire(label, score)

    def _maybe_fire(self, label: str, score: float) -> None:
        now = time.monotonic()
        if now - self._last_fire_at < self._cooldown_s:
            return
        self._last_fire_at = now
        logger.info("Wake word fired: label=%s score=%.3f", label, score)
        try:
            asyncio.run_coroutine_threadsafe(self._on_wake(label, score), self._loop)
        except RuntimeError as exc:
            logger.debug("Cannot schedule wake-word callback: %s", exc)


def _build_openwakeword_backend() -> Optional[OpenWakeWordBackend]:
    model_path = (os.getenv(ENV_OWW_MODEL) or "").strip()
    if not model_path:
        logger.warning("%s not set; openWakeWord backend disabled", ENV_OWW_MODEL)
        return None
    resolved = Path(model_path).expanduser().resolve()
    if not resolved.exists():
        logger.warning("openWakeWord model %s not found; backend disabled", resolved)
        return None
    threshold = _float_env(ENV_OWW_THRESHOLD, DEFAULT_OWW_THRESHOLD)
    framework = (os.getenv(ENV_OWW_FRAMEWORK) or "onnx").strip().lower()
    return OpenWakeWordBackend(model_path=str(resolved), threshold=threshold, framework=framework)


def build_wake_word_detector(
    on_wake: WakeCallback,
    loop: asyncio.AbstractEventLoop,
) -> Optional[WakeWordDetector]:
    """Construct a detector from env config, or return None if disabled."""
    if not _is_truthy(os.getenv(ENV_ENABLED)):
        return None

    engine = (os.getenv(ENV_ENGINE) or DEFAULT_ENGINE).strip().lower()
    cooldown = _float_env(ENV_COOLDOWN, DEFAULT_COOLDOWN_S)

    backend: Optional[WakeWordBackend]
    if engine == "vosk":
        from reachy_mini_conversation_app.wake_word.vosk_backend import build_vosk_backend
        backend = build_vosk_backend()
    elif engine in {"openwakeword", "oww"}:
        backend = _build_openwakeword_backend()
    else:
        logger.warning("Unknown wake-word engine %r; detection disabled", engine)
        return None

    if backend is None:
        return None
    return WakeWordDetector(backend=backend, on_wake=on_wake, loop=loop, cooldown_s=cooldown)
