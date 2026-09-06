"""openWakeWord backend.

The only place in `services/voice` that may import openwakeword, and it does so inside a
method so the package imports on a machine with no weights. Nothing here is verified
against real weights yet — see the report in services/voice/README.md.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import numpy as np

from ..audio.frames import pcm_to_float32
from .base import BufferedWakewordEngine

log = logging.getLogger(__name__)


class OpenWakeWordEngine(BufferedWakewordEngine):
    """Always-on, on-device, ~a few ms of CPU per 80 ms window on Apple Silicon.

    Model resolution order:
      1. `model_path` if given,
      2. `<model_dir>/<keyword>.onnx` / `.tflite`,
      3. the keyword as an openwakeword built-in name.
    """

    name = "openwakeword"

    def __init__(
        self,
        *,
        model_dir: Path | str = "./models/wakeword",
        model_path: Path | str | None = None,
        inference_framework: str = "onnx",
        vad_threshold: float = 0.0,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._model_dir = Path(model_dir)
        self._model_path = Path(model_path) if model_path else None
        self._inference_framework = inference_framework
        self._vad_threshold = vad_threshold
        self._model = None
        self._load_lock = asyncio.Lock()

    def _resolve_model(self) -> str:
        if self._model_path is not None:
            return str(self._model_path)
        for suffix in (".onnx", ".tflite"):
            candidate = self._model_dir / f"{self.keyword}{suffix}"
            if candidate.is_file():
                return str(candidate)
        return self.keyword  # built-in name; openwakeword downloads on first use

    async def _load(self) -> None:
        if self._model is not None:
            return
        async with self._load_lock:
            if self._model is not None:
                return
            try:
                from openwakeword.model import Model
            except ImportError as exc:
                raise RuntimeError(
                    "OpenWakeWordEngine needs the 'wakeword' extra: "
                    "uv pip install -e 'services/voice[wakeword]', then "
                    "scripts/fetch_voice_models.sh"
                ) from exc

            target = self._resolve_model()
            log.info("loading openwakeword model %s (%s)", target, self._inference_framework)
            # Loading is 100+ ms of blocking work; doing it on the event loop would stall
            # the audio queue and show up as a frame gap at startup.
            self._model = await asyncio.to_thread(
                Model,
                wakeword_models=[target],
                inference_framework=self._inference_framework,
                vad_threshold=self._vad_threshold,
            )

    async def _score_window(self, pcm: bytes) -> float:
        if self._model is None:  # pragma: no cover - _load runs first
            await self._load()
        samples = (pcm_to_float32(pcm) * 32767.0).astype(np.int16)
        # predict() is a small ONNX graph: microseconds-to-low-milliseconds. Running it
        # inline keeps detection latency deterministic; a thread hop per 80 ms window costs
        # more than the inference itself.
        scores = self._model.predict(samples)  # type: ignore[union-attr]
        if not scores:
            return 0.0
        return float(max(scores.values()))

    def reset(self) -> None:
        super().reset()
        if self._model is not None:  # pragma: no cover - needs weights
            self._model.reset()
