"""Mock wakeword — a complete engine, not a stub.

It runs the same pre-roll, refractory and evaluation code paths as the real backend; only
the scoring function is replaced. That is deliberate: the bugs that actually bite (clipped
first syllable, triple-fire, unmonotonic seq after splicing) live in the shared code, so a
mock that bypasses it would test nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from ars_protocol import AudioFrame

from ..audio.frames import rms_dbfs
from .base import BufferedWakewordEngine


class MockWakewordEngine(BufferedWakewordEngine):
    """Fires deterministically.

    Three ways to drive it, in priority order:

    * `trigger_seqs` — fire on the inference window containing these frame `seq` values.
      Exact and repeatable; this is what the unit tests use.
    * `score_fn` — arbitrary scoring over the window's PCM.
    * default — fire when the window is louder than `energy_trigger_db`. Makes the mock
      usable against real microphone audio for a manual end-to-end run (clap to wake).
    """

    name = "mock"

    def __init__(
        self,
        *,
        trigger_seqs: Iterable[int] | None = None,
        score_fn: Callable[[bytes], float] | None = None,
        energy_trigger_db: float = -30.0,
        **kwargs,
    ) -> None:
        kwargs.setdefault("keyword", "hey_ars")
        super().__init__(**kwargs)
        self._trigger_seqs = set(trigger_seqs or ())
        self._score_fn = score_fn
        self._energy_trigger_db = energy_trigger_db
        self._window_seqs: list[int] = []

    async def detect(self, frames, /):  # type: ignore[override]
        # Wrap the frame stream to remember which seqs are in the current window, so
        # `trigger_seqs` can be evaluated inside `_score_window`.
        async def tracked():
            async for frame in frames:
                self._window_seqs.append(frame.seq)
                yield frame

        async for event in super().detect(tracked()):
            yield event

    async def _score_window(self, pcm: bytes) -> float:
        window_seqs = self._window_seqs
        self._window_seqs = []
        if self._trigger_seqs:
            return 1.0 if self._trigger_seqs.intersection(window_seqs) else 0.0
        if self._score_fn is not None:
            return self._score_fn(pcm)
        return 1.0 if rms_dbfs(pcm) > self._energy_trigger_db else 0.0

    def reset(self) -> None:
        super().reset()
        self._window_seqs = []


class NullWakewordEngine(BufferedWakewordEngine):
    """Never fires. For push-to-talk clients and for the phone in a pocket, where an
    always-on microphone is the wrong default."""

    name = "null"

    async def _score_window(self, pcm: bytes) -> float:
        return 0.0

    @property
    def false_accepts_per_hour(self) -> float:
        """Exactly zero, and this one *is* a measurement: the engine cannot fire."""
        return 0.0


def always_frames(frames: Iterable[AudioFrame]) -> list[AudioFrame]:
    return list(frames)
