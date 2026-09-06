"""Silero VAD backend.

The only place silero/onnxruntime may be imported. Silero v5 at 16 kHz insists on exactly
512-sample windows; protocol frames are 320 samples, so frames are accumulated and the most
recent window's probability is held over. That introduces up to ~12 ms of lag on the VAD
decision, which is inside the noise of the 400 ms endpointing budget and is documented here
so nobody rediscovers it as a mystery.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from ars_protocol import SAMPLE_RATE_HZ, AudioFrame

from ..audio.frames import pcm_to_float32
from .base import FrameVadEngine

log = logging.getLogger(__name__)

SILERO_WINDOW_SAMPLES = 512
"""Fixed by the model at 16 kHz. Not a tunable."""


class SileroVadEngine(FrameVadEngine):
    name = "silero"

    def __init__(
        self,
        *,
        threshold: float = 0.5,
        model_dir: Path | str = "./models/vad",
        onset_ms: float = 60.0,
        hangover_ms: float = 200.0,
    ) -> None:
        super().__init__(onset_ms=onset_ms, hangover_ms=hangover_ms)
        self.threshold = threshold
        self._model_dir = Path(model_dir)
        self._model = None
        self._to_tensor = None
        self._buffer = np.zeros(0, dtype=np.float32)
        self._last_probability = 0.0

    async def prepare(self) -> None:
        if self._model is not None:
            return
        import asyncio

        self._model = await asyncio.to_thread(self._load_model)

    def _load_model(self):
        try:
            import torch
            from silero_vad import load_silero_vad
            from silero_vad.utils_vad import OnnxWrapper
        except ImportError as exc:
            raise RuntimeError(
                "SileroVadEngine needs the 'vad' extra: uv pip install -e 'services/voice[vad]'. "
                "The energy VAD is the supported no-weights fallback."
            ) from exc

        # Even in ONNX mode the wrapper calls `x.dim()`, so the window has to arrive as a
        # torch tensor, not the numpy array every other engine here takes.
        self._to_tensor = torch.from_numpy

        local = self._model_dir / "silero_vad.onnx"
        if local.is_file():
            # Load the copy scripts/fetch_voice_models.sh put in models/, not the one bundled
            # in the wheel: the reference deployment is offline and pins its own weights.
            # `load_silero_vad` grew no path argument in silero-vad 6.x, so go to the wrapper
            # it uses internally.
            log.info("loading silero VAD from %s", local)
            return OnnxWrapper(str(local), force_onnx_cpu=True)
        log.info("loading silero VAD from package defaults")
        return load_silero_vad(onnx=True)

    def probability(self, frame: AudioFrame) -> float:
        if self._model is None:
            raise RuntimeError("SileroVadEngine.prepare() must be awaited before use")
        self._buffer = np.concatenate([self._buffer, pcm_to_float32(frame.pcm)])
        while self._buffer.size >= SILERO_WINDOW_SAMPLES:
            window = self._buffer[:SILERO_WINDOW_SAMPLES]
            self._buffer = self._buffer[SILERO_WINDOW_SAMPLES:]
            tensor = self._to_tensor(np.ascontiguousarray(window))
            self._last_probability = float(self._model(tensor, SAMPLE_RATE_HZ).item())
        # Rescale around the configured threshold so FrameVadEngine's 0.5 cut is honoured.
        if self.threshold <= 0:
            return 1.0
        if self._last_probability >= self.threshold:
            excess = (self._last_probability - self.threshold) / max(1e-6, 1 - self.threshold)
            return 0.5 + 0.5 * min(1.0, excess)
        return 0.5 * (self._last_probability / self.threshold)

    def reset(self) -> None:
        super().reset()
        self._buffer = np.zeros(0, dtype=np.float32)
        self._last_probability = 0.0
        if self._model is not None and hasattr(self._model, "reset_states"):  # pragma: no cover
            self._model.reset_states()
