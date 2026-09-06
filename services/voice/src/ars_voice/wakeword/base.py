"""Shared wakeword machinery: pre-roll ring buffer, refractory period, honest FA/hour.

Backends implement `_score_window`. Everything else — and in particular the pre-roll,
which is the difference between "send an email to Andrei" and "mail to Andrei" — lives
here so no backend can forget it.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path

from ars_core import WakewordEngine
from ars_protocol import FRAME_MS, AudioFrame, WakeEvent, now_ms

from ..audio.frames import PreRollBuffer, frame_duration_ms, frames_for_ms, validate_frame
from ..metrics import EngineCounters
from .evaluation import (
    NOT_EVALUATED,
    NotEvaluatedError,
    WakewordEvaluation,
    format_evaluation,
    load_evaluation,
)

log = logging.getLogger(__name__)

DEFAULT_INFERENCE_WINDOW_MS = 80
"""openwakeword's models want 1280 samples (80 ms) per call. Frames are 20 ms, so four
frames are accumulated per inference. Kept as a multiple of FRAME_MS by assertion below."""


class BufferedWakewordEngine(WakewordEngine):
    """Base for every wakeword backend.

    Detection latency is measured from the *end of the audio window that triggered it* to
    the moment the event is yielded — that is the number the budget table means by
    "wakeword detection: 150 ms". Measuring from the start of the keyword instead would
    make the model's own lookback look like our latency.
    """

    name = "buffered"

    def __init__(
        self,
        *,
        keyword: str = "hey_ars",
        threshold: float = 0.6,
        pre_roll_ms: int = 500,
        refractory_ms: int = 1_500,
        inference_window_ms: int = DEFAULT_INFERENCE_WINDOW_MS,
        benchmark_dir: Path | str = "./research/benchmarks/wakeword",
        counters: EngineCounters | None = None,
    ) -> None:
        if inference_window_ms % FRAME_MS:
            raise ValueError(
                f"inference_window_ms={inference_window_ms} must be a multiple of "
                f"FRAME_MS={FRAME_MS} (ars_protocol.audio)"
            )
        self.keyword = keyword
        self.threshold = threshold
        self.pre_roll_ms = pre_roll_ms
        self.refractory_ms = refractory_ms
        self._window_frames = max(1, frames_for_ms(inference_window_ms))
        self._benchmark_dir = Path(benchmark_dir)
        self.counters = counters or EngineCounters()

        # Retain a little more than requested: the caller asks for `pre_roll_ms` of audio
        # *before* detection, and detection lands up to one inference window late.
        self._pre_roll = PreRollBuffer(pre_roll_ms + inference_window_ms + FRAME_MS)
        self._window: deque[AudioFrame] = deque(maxlen=self._window_frames)
        self._pending: list[tuple[WakeEvent, tuple[AudioFrame, ...]]] = []
        self._last_fire_ms: float | None = None
        self._elapsed_ms: float = 0.0
        self._evaluation: WakewordEvaluation | bool | None = False  # False = not yet loaded

    # ------------------------------------------------------------------ backend hook

    @abstractmethod
    async def _score_window(self, pcm: bytes) -> float:
        """Score `inference_window_ms` of audio in [0, 1]."""

    async def _load(self) -> None:
        """Optional lazy model load, called on the first frame. Backends override."""
        return None

    # ------------------------------------------------------------------ interface

    async def detect(self, frames: AsyncIterator[AudioFrame]) -> AsyncIterator[WakeEvent]:
        await self._load()
        self.reset()
        expected_seq: int | None = None
        async for frame in frames:
            validate_frame(frame)
            self.counters.frames_in += 1
            if expected_seq is not None and frame.seq != expected_seq:
                # A seq gap is dropped audio. It degrades detection silently otherwise.
                self.counters.frame_gaps += 1
                log.warning("wakeword: frame gap, expected seq=%d got %d", expected_seq, frame.seq)
            expected_seq = frame.seq + 1

            self._pre_roll.push(frame)
            self._window.append(frame)
            self._elapsed_ms += frame_duration_ms(frame)

            if len(self._window) < self._window_frames:
                continue

            score = await self._score_window(b"".join(f.pcm for f in self._window))
            self._window.clear()

            if score < self.threshold or self._in_refractory():
                continue

            self._last_fire_ms = self._elapsed_ms
            event = self._make_event(score)
            self.counters.wake_events += 1
            yield event

    def _make_event(self, score: float) -> WakeEvent:
        """Snapshot the ring buffer at detection time and attach it to the event.

        The snapshot is taken here, not when the consumer gets around to asking, because
        by then the ring has rotated and the pre-roll would be audio from *after* the
        wakeword — which is exactly the bug this whole mechanism exists to prevent.
        """
        available_ms = self._pre_roll.duration_ms
        retained_ms = int(min(self.pre_roll_ms, available_ms))
        snapshot = self._pre_roll.snapshot(retained_ms)
        event = WakeEvent(
            keyword=self.keyword,
            score=min(max(score, 0.0), 1.0),
            detected_at_ms=now_ms(),
            pre_roll_ms=retained_ms,
        )
        self._pending.append((event, snapshot))
        if len(self._pending) > 8:
            del self._pending[:-8]
        return event

    def pre_roll_frames(self, event: WakeEvent) -> tuple[AudioFrame, ...]:
        """The audio captured before `event` fired, oldest first.

        Feed this to ASR ahead of the live stream. The frames are returned with their
        original `seq`; use `ars_voice.audio.reseq` when splicing so the merged stream stays
        monotonic and the gap detector does not cry wolf.
        """
        for pending_event, frames in reversed(self._pending):
            if pending_event is event or pending_event == event:
                return frames
        return ()

    def reset(self) -> None:
        self._pre_roll.clear()
        self._window.clear()
        self._pending.clear()
        self._last_fire_ms = None
        self._elapsed_ms = 0.0

    def _in_refractory(self) -> bool:
        if self._last_fire_ms is None:
            return False
        return (self._elapsed_ms - self._last_fire_ms) < self.refractory_ms

    # ------------------------------------------------------------------ honesty

    @property
    def evaluation(self) -> WakewordEvaluation | None:
        if self._evaluation is False:
            self._evaluation = load_evaluation(self._benchmark_dir, self.name, self.keyword)
        return self._evaluation  # type: ignore[return-value]

    @property
    def false_accepts_per_hour(self) -> float:
        """Measured FA/hour, or NaN if this engine has never been evaluated.

        NaN, not 0.0, and not a number copied from a vendor's README. A caller that wants
        to refuse to run an unmeasured wakeword should call `assert_shippable()`.
        """
        evaluation = self.evaluation
        if evaluation is None:
            log.warning(
                "%s/%s has no evaluation record in %s — FA/hour is unknown. "
                "Run ars-voice-wakeword-eval before shipping.",
                self.name, self.keyword, self._benchmark_dir,
            )
            return NOT_EVALUATED
        if abs(evaluation.threshold - self.threshold) > 1e-9:
            # FA/hour is a function of the threshold. Reporting a number measured at a
            # different operating point is worse than reporting nothing.
            log.warning(
                "%s/%s evaluated at threshold %.3f but running at %.3f — FA/hour not applicable",
                self.name, self.keyword, evaluation.threshold, self.threshold,
            )
            return NOT_EVALUATED
        return evaluation.false_accepts_per_hour

    @property
    def false_reject_rate(self) -> float:
        evaluation = self.evaluation
        return NOT_EVALUATED if evaluation is None else evaluation.false_reject_rate

    def assert_shippable(self, *, max_fa_per_hour: float = 0.5) -> None:
        """Refuse to run an unmeasured or too-trigger-happy wakeword."""
        evaluation = self.evaluation
        if evaluation is None:
            raise NotEvaluatedError(
                f"{self.name}/{self.keyword}: no evaluation record in {self._benchmark_dir}. "
                "An unmeasured wakeword can open the microphone at any time."
            )
        fa = self.false_accepts_per_hour
        if not (fa <= max_fa_per_hour):
            raise NotEvaluatedError(
                f"{self.name}/{self.keyword}: FA/hour={fa} exceeds limit {max_fa_per_hour}"
            )

    def describe(self) -> str:
        return format_evaluation(self.evaluation)
