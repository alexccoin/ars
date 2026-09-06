"""VAD base: per-frame speech probability plus onset/hangover smoothing.

Backends implement `probability(frame)`. The `step()` API is synchronous because the
pipeline needs a VAD verdict *and* the frame itself, at the same time, to feed ASR — an
async-iterator-only VAD forces every caller to fan the audio stream out by hand and get
the buffering wrong.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass

from ars_core import VadEngine
from ars_protocol import FRAME_MS, AudioFrame, SpeechBoundary, VadEvent, now_ms

from ..audio.frames import frame_duration_ms, rms_dbfs


@dataclass(frozen=True)
class VadStep:
    is_speech: bool
    """Smoothed: onset-confirmed and hangover-extended. Use for barge-in and for the
    protocol's SPEECH_START/SPEECH_END events."""
    probability: float
    energy_db: float
    raw_is_speech: bool = False
    """Unsmoothed, this frame only. **This is what endpointing consumes.**

    Feeding the smoothed decision to the endpointer stacks the VAD hangover on top of the
    endpoint silence window, so a 700 ms setting waits 900 ms — measured, see
    `ars-voice-endpoint-eval --sweep`. The endpointer's own silence window is already the
    mechanism that bridges pauses inside speech; the hangover is for event reporting."""
    event: VadEvent | None = None


class FrameVadEngine(VadEngine):
    """Common smoothing for every VAD backend.

    `onset_ms` suppresses single-frame blips; `hangover_ms` keeps SPEECH asserted across the
    natural gaps inside a word (a stop consonant is 40-80 ms of near-silence, and a VAD
    without hangover reports the middle of "asculta" as the end of the utterance).
    """

    name = "frame"

    def __init__(self, *, onset_ms: float = 60.0, hangover_ms: float = 200.0) -> None:
        self.onset_ms = onset_ms
        self.hangover_ms = hangover_ms
        self._in_speech = False
        self._onset_run_ms = 0.0
        self._hangover_left_ms = 0.0

    @abstractmethod
    def probability(self, frame: AudioFrame) -> float:
        """Raw per-frame speech probability in [0, 1], before smoothing."""

    def raw_is_speech(self, frame: AudioFrame) -> bool:
        return self.probability(frame) >= 0.5

    async def prepare(self) -> None:
        """Lazy model load. No-op for backends that need no weights."""
        return None

    def reset(self) -> None:
        self._in_speech = False
        self._onset_run_ms = 0.0
        self._hangover_left_ms = 0.0

    def step(self, frame: AudioFrame) -> VadStep:
        duration = frame_duration_ms(frame) or float(FRAME_MS)
        probability = self.probability(frame)
        raw = probability >= 0.5
        energy_db = rms_dbfs(frame.pcm)
        event: VadEvent | None = None

        if raw:
            self._hangover_left_ms = self.hangover_ms
            if not self._in_speech:
                self._onset_run_ms += duration
                if self._onset_run_ms >= self.onset_ms:
                    self._in_speech = True
                    event = VadEvent(
                        boundary=SpeechBoundary.SPEECH_START,
                        at_ms=now_ms(),
                        energy_db=energy_db,
                    )
        else:
            self._onset_run_ms = 0.0
            if self._in_speech:
                self._hangover_left_ms -= duration
                if self._hangover_left_ms <= 0:
                    self._in_speech = False
                    event = VadEvent(
                        boundary=SpeechBoundary.SPEECH_END,
                        at_ms=now_ms(),
                        energy_db=energy_db,
                    )

        return VadStep(
            is_speech=self._in_speech,
            probability=probability,
            energy_db=energy_db,
            raw_is_speech=raw,
            event=event,
        )

    async def process(self, frames: AsyncIterator[AudioFrame]) -> AsyncIterator[VadEvent]:
        await self.prepare()
        self.reset()
        async for frame in frames:
            step = self.step(frame)
            if step.event is not None:
                yield step.event
