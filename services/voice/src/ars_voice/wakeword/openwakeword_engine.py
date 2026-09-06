"""openWakeWord backend.

The only place in `services/voice` that may import openwakeword, and it does so inside a
method so the package imports on a machine with no weights. Nothing here is verified
against real weights yet — see the report in services/voice/README.md.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import numpy as np

from ..audio.frames import pcm_to_float32
from .base import BufferedWakewordEngine

log = logging.getLogger(__name__)


SHARED_FRONT_END = frozenset({"embedding_model", "melspectrogram", "silero_vad"})
"""Not keywords. openWakeWord ships one melspectrogram + embedding front end that every
keyword model sits on top of; listing them as available wakewords would send someone off to
configure ARS_WAKEWORD=melspectrogram."""

_VERSION_SUFFIX = re.compile(r"_v\d+(\.\d+)*$")


def available_keywords(model_dir: Path | str) -> tuple[str, ...]:
    """Keywords with a model actually on disk, version suffixes stripped.

    openWakeWord names its files `hey_jarvis_v0.1.onnx`; users configure `hey_jarvis`.
    """
    directory = Path(model_dir)
    if not directory.is_dir():
        return ()
    names = set()
    for path in directory.iterdir():
        if path.suffix not in {".onnx", ".tflite"}:
            continue
        stem = _VERSION_SUFFIX.sub("", path.stem)
        if stem not in SHARED_FRONT_END:
            names.add(stem)
    return tuple(sorted(names))


class MissingWakewordModel(FileNotFoundError):
    """The configured keyword has no model. Raised at load, with the list of what is there.

    Failing loudly matters more here than it looks: openWakeWord silently treats an unknown
    name as a hub model id and tries to download it. On a machine with no network that hangs;
    on a machine with network it fetches something nobody asked for. Neither is acceptable in
    the always-on, pre-consent part of the system.
    """


class OpenWakeWordEngine(BufferedWakewordEngine):
    """Always-on, on-device, ~a few ms of CPU per 80 ms window on Apple Silicon.

    Model resolution order:
      1. `model_path` if given,
      2. `<model_dir>/<keyword>*.onnx` / `.tflite` (matches openWakeWord's `_vN.N` suffixes),
      3. failure, listing the keywords that *are* present.

    There is no `hey_ars` model — a custom keyword has to be trained. Until then the
    deployment picks a stock one (`ARS_WAKEWORD=hey_jarvis`; `alexa` would fire every time
    somebody in the house talks to an Echo).
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
            if not Path(self._model_path).is_file():
                raise MissingWakewordModel(f"no wakeword model at {self._model_path}")
            return str(self._model_path)

        preferred = ".onnx" if self._inference_framework == "onnx" else ".tflite"
        for suffix in (preferred, ".onnx", ".tflite"):
            exact = self._model_dir / f"{self.keyword}{suffix}"
            if exact.is_file():
                return str(exact)
            versioned = sorted(self._model_dir.glob(f"{self.keyword}_v*{suffix}"))
            if versioned:
                return str(versioned[-1])

        available = available_keywords(self._model_dir)
        raise MissingWakewordModel(
            f"no wakeword model for ARS_WAKEWORD={self.keyword!r} in {self._model_dir}. "
            + (
                f"Available: {', '.join(available)}. "
                if available
                else "That directory holds no keyword models — run scripts/fetch_voice_models.sh. "
            )
            + "openWakeWord ships no 'hey_ars'; training one is tracked in research/notes."
        )

    @property
    def available_keywords(self) -> tuple[str, ...]:
        return available_keywords(self._model_dir)

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
