"""Energy VAD — the fallback that must genuinely work with no model on disk.

This is not a placeholder. It is the VAD A.R.S runs when silero weights are absent, on a
phone with no room for them, or when onnxruntime will not load. It therefore has to cope
with a real room, which means the fixed dBFS threshold is the wrong primary mechanism:
a quiet office floor sits near -65 dBFS, a kitchen with a fridge near -45.

So the threshold floats above a tracked noise floor. The floor rises fast and falls slowly
— speech must not be able to drag the floor up to its own level and mute the VAD.
"""

from __future__ import annotations

import math

from ars_protocol import FRAME_MS, AudioFrame

from ..audio.frames import SILENCE_DBFS, frame_duration_ms, rms_dbfs
from .base import FrameVadEngine


class EnergyVadEngine(FrameVadEngine):
    """Adaptive-threshold energy VAD. No weights, no imports, no excuses."""

    name = "energy"

    def __init__(
        self,
        *,
        threshold_db: float = -42.0,
        adaptive: bool = True,
        noise_margin_db: float = 9.0,
        noise_floor_halflife_ms: float = 1_500.0,
        min_floor_db: float = -75.0,
        max_floor_db: float = -30.0,
        speech_floor_timeout_ms: float = 8_000.0,
        onset_ms: float = 60.0,
        hangover_ms: float = 200.0,
    ) -> None:
        super().__init__(onset_ms=onset_ms, hangover_ms=hangover_ms)
        self.threshold_db = threshold_db
        self.adaptive = adaptive
        self.noise_margin_db = noise_margin_db
        self.noise_floor_halflife_ms = max(noise_floor_halflife_ms, 1.0)
        self.min_floor_db = min_floor_db
        self.max_floor_db = max_floor_db
        self.speech_floor_timeout_ms = speech_floor_timeout_ms
        """Nobody speaks for eight seconds without pausing. Past this the 'speech' is a
        television, a fan or an extractor hood, and the noise floor is allowed to rise to
        meet it — otherwise the floor freezes at the pre-noise level and the VAD reports
        speech forever."""
        self._noise_floor_db: float | None = None
        self._last_energy_db: float = SILENCE_DBFS
        self._speech_run_ms: float = 0.0

    @property
    def noise_floor_db(self) -> float:
        if self._noise_floor_db is None:
            return self.threshold_db - self.noise_margin_db
        return self._noise_floor_db

    @property
    def effective_threshold_db(self) -> float:
        """The level a frame must beat to count as speech."""
        if not self.adaptive or self._noise_floor_db is None:
            return self.threshold_db
        return max(
            self._noise_floor_db + self.noise_margin_db,
            self.min_floor_db + self.noise_margin_db,
        )

    def reset(self) -> None:
        super().reset()
        self._noise_floor_db = None
        self._last_energy_db = SILENCE_DBFS
        self._speech_run_ms = 0.0

    def probability(self, frame: AudioFrame) -> float:
        energy_db = rms_dbfs(frame.pcm)
        self._last_energy_db = energy_db
        duration = frame_duration_ms(frame) or float(FRAME_MS)
        threshold = self.effective_threshold_db
        is_speech = energy_db > threshold
        self._speech_run_ms = self._speech_run_ms + duration if is_speech else 0.0
        self._update_floor(energy_db, duration, is_speech=is_speech)
        # Map dB distance from the threshold onto [0, 1] over a 12 dB span, so callers that
        # want a soft score (barge-in wants a margin, not a boolean) get one.
        return 1.0 / (1.0 + math.exp(-(energy_db - threshold) / 3.0))

    def _update_floor(self, energy_db: float, duration_ms: float, *, is_speech: bool) -> None:
        if energy_db <= SILENCE_DBFS:
            return
        if self._noise_floor_db is None:
            self._noise_floor_db = min(max(energy_db, self.min_floor_db), self.max_floor_db)
            return
        # Asymmetric: adapt quickly to a quieter room, slowly to a louder one, and not at
        # all (upward) while speech is present.
        if energy_db < self._noise_floor_db:
            alpha = 1.0 - 0.5 ** (duration_ms / (self.noise_floor_halflife_ms / 4))
        elif is_speech and self._speech_run_ms < self.speech_floor_timeout_ms:
            alpha = 0.0
        else:
            alpha = 1.0 - 0.5 ** (duration_ms / self.noise_floor_halflife_ms)
        floor = self._noise_floor_db + alpha * (energy_db - self._noise_floor_db)
        self._noise_floor_db = min(max(floor, self.min_floor_db), self.max_floor_db)
