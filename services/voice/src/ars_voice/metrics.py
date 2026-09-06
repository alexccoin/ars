"""Latency instrumentation.

The budget table in docs/architecture/overview.md is transcribed here once, with its
owner, so a stage can report against it without anyone re-typing a number. Four of the six
rows belong to voice-engineer; the other two are recorded when the pipeline is wired to a
real compute service, and reported but never asserted by this package.

Everything is measured with `time.perf_counter_ns` (monotonic). Protocol timestamps use
wall clock `now_ms`, which is right for the wire and wrong for measuring durations.
"""

from __future__ import annotations

import math
import statistics
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum


class Stage(StrEnum):
    """One row of the latency budget, plus the sub-measurements voice cares about."""

    WAKEWORD_DETECTION = "wakeword_detection"
    ENDPOINTING = "endpointing"
    ASR_FINAL = "asr_final"
    CONTEXT_ASSEMBLY = "context_assembly"
    LLM_FIRST_TOKEN = "llm_first_token"  # noqa: S105 - a budget row, not a credential
    TTS_FIRST_AUDIO = "tts_first_audio"
    TOTAL = "total"
    # Not budget rows. Tracked because they are how the budget rows go wrong.
    ASR_FIRST_PARTIAL = "asr_first_partial"
    BARGE_IN_CANCEL = "barge_in_cancel"


BUDGET_P95_MS: dict[Stage, float | None] = {
    Stage.WAKEWORD_DETECTION: 150.0,
    # 700 ms silence window + 50 ms detection overhead. The window is a deliberate
    # cost, not slack: at 400 ms the endpointer truncates 2 of 12 fixtures, and cutting
    # the user off mid-sentence is a worse experience than waiting a beat. Lowering
    # this is a job for speculative decode (ADR 0001), not for a smaller number here.
    Stage.ENDPOINTING: 750.0,
    Stage.ASR_FINAL: 250.0,
    Stage.CONTEXT_ASSEMBLY: 80.0,
    Stage.LLM_FIRST_TOKEN: 200.0,
    Stage.TTS_FIRST_AUDIO: 120.0,
    # speech-end -> first audio out. Phase 1 (sequential): 750+250+80+200+120.
    # Drops to ~1050 once ASR final and LLM prefill overlap the silence window.
    Stage.TOTAL: 1400.0,
    Stage.ASR_FIRST_PARTIAL: None,
    Stage.BARGE_IN_CANCEL: None,
}

STAGE_OWNER: dict[Stage, str] = {
    Stage.WAKEWORD_DETECTION: "voice-engineer",
    Stage.ENDPOINTING: "voice-engineer",
    Stage.ASR_FINAL: "voice-engineer",
    Stage.CONTEXT_ASSEMBLY: "backend / ml",
    Stage.LLM_FIRST_TOKEN: "ml-engineer",
    Stage.TTS_FIRST_AUDIO: "voice-engineer",
    Stage.TOTAL: "system-architect",
    Stage.ASR_FIRST_PARTIAL: "voice-engineer",
    Stage.BARGE_IN_CANCEL: "voice-engineer",
}

VOICE_OWNED: tuple[Stage, ...] = (
    Stage.WAKEWORD_DETECTION,
    Stage.ENDPOINTING,
    Stage.ASR_FINAL,
    Stage.TTS_FIRST_AUDIO,
)


def _now_ms() -> float:
    return time.perf_counter_ns() / 1e6


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile.

    Not interpolated: with the sample counts a voice pipeline realistically collects
    (tens, not millions) interpolation invents a value that was never measured, and p95 is
    a promise about observed behaviour.
    """
    if not values:
        return math.nan
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


@dataclass(frozen=True)
class StageStats:
    stage: Stage
    count: int
    p50_ms: float
    p95_ms: float
    max_ms: float
    budget_ms: float | None
    owner: str

    @property
    def within_budget(self) -> bool | None:
        """None means 'no budget declared for this row', not 'passed'."""
        if self.budget_ms is None or self.count == 0:
            return None
        return self.p95_ms <= self.budget_ms

    @property
    def headroom_ms(self) -> float:
        if self.budget_ms is None:
            return math.nan
        return self.budget_ms - self.p95_ms


@dataclass
class TurnLatency:
    """Timestamps for one turn, in monotonic ms. `None` means the stage did not happen —
    a barged-in turn has no TTS completion and pretending otherwise skews every p95."""

    turn_id: str | None = None
    wake_audio_ms: float | None = None
    wake_detected_ms: float | None = None
    speech_end_ms: float | None = None
    endpoint_ms: float | None = None
    asr_first_partial_ms: float | None = None
    asr_final_ms: float | None = None
    reply_first_token_ms: float | None = None
    tts_requested_ms: float | None = None
    tts_first_audio_ms: float | None = None
    cancel_requested_ms: float | None = None
    cancel_effective_ms: float | None = None
    cancelled: bool = False
    committed: bool = False
    """Set by the recorder. A turn folded into the distributions twice makes p95 a
    function of how many code paths happened to call commit(), not of what the user felt."""

    def _total(self) -> float | None:
        """The budget's 'wakeword to first audio', excluding the time the user spends talking.

        The table's rows sum to 1200 ms, and none of them is "length of the utterance" — so
        total is the wakeword span plus everything from the moment the user stops speaking to
        the moment audio comes back. Measuring wall clock from the wakeword instead would add
        the user's own speech to A.R.S's latency, which is both wrong and unfixable.
        """
        if self.wake_audio_ms is None or self.wake_detected_ms is None:
            return None
        if self.speech_end_ms is None or self.tts_first_audio_ms is None:
            return None
        return (self.wake_detected_ms - self.wake_audio_ms) + (
            self.tts_first_audio_ms - self.speech_end_ms
        )

    def durations(self) -> dict[Stage, float]:
        out: dict[Stage, float] = {}

        def span(a: float | None, b: float | None) -> float | None:
            return None if a is None or b is None else b - a

        pairs: list[tuple[Stage, float | None]] = [
            (Stage.WAKEWORD_DETECTION, span(self.wake_audio_ms, self.wake_detected_ms)),
            (Stage.ENDPOINTING, span(self.speech_end_ms, self.endpoint_ms)),
            (Stage.ASR_FINAL, span(self.endpoint_ms, self.asr_final_ms)),
            (Stage.ASR_FIRST_PARTIAL, span(self.wake_detected_ms, self.asr_first_partial_ms)),
            (Stage.LLM_FIRST_TOKEN, span(self.asr_final_ms, self.reply_first_token_ms)),
            (Stage.TTS_FIRST_AUDIO, span(self.tts_requested_ms, self.tts_first_audio_ms)),
            (Stage.BARGE_IN_CANCEL, span(self.cancel_requested_ms, self.cancel_effective_ms)),
            (Stage.TOTAL, self._total()),
        ]
        for stage, value in pairs:
            if value is not None and value >= 0:
                out[stage] = value
        return out


class LatencyRecorder:
    """Collects per-turn timings and reports p50/p95 against the budget.

    Not optional and not sampled: a voice pipeline whose latency is only measured when
    someone remembers to measure it will regress on the day nobody is looking.
    """

    def __init__(self, *, keep: int = 500) -> None:
        self._keep = keep
        self._samples: dict[Stage, list[float]] = defaultdict(list)
        self._turns: list[TurnLatency] = []

    def start_turn(self, turn_id: str | None = None) -> TurnLatency:
        turn = TurnLatency(turn_id=turn_id)
        self._turns.append(turn)
        if len(self._turns) > self._keep:
            self._turns = self._turns[-self._keep :]
        return turn

    @staticmethod
    def mark() -> float:
        """Monotonic 'now' in ms, for filling in a TurnLatency field."""
        return _now_ms()

    def commit(self, turn: TurnLatency) -> dict[Stage, float]:
        """Fold a finished turn's spans into the distributions."""
        if turn.committed:
            return {}
        turn.committed = True
        durations = turn.durations()
        for stage, value in durations.items():
            bucket = self._samples[stage]
            bucket.append(value)
            if len(bucket) > self._keep:
                del bucket[: len(bucket) - self._keep]
        return durations

    def record(self, stage: Stage, ms: float) -> None:
        """Record a single span directly, for engines measured outside a full turn."""
        bucket = self._samples[stage]
        bucket.append(ms)
        if len(bucket) > self._keep:
            del bucket[: len(bucket) - self._keep]

    @contextmanager
    def time(self, stage: Stage) -> Iterator[None]:
        start = _now_ms()
        try:
            yield
        finally:
            self.record(stage, _now_ms() - start)

    def samples(self, stage: Stage) -> tuple[float, ...]:
        return tuple(self._samples.get(stage, ()))

    def stats(self, stage: Stage) -> StageStats:
        values = self._samples.get(stage, [])
        return StageStats(
            stage=stage,
            count=len(values),
            p50_ms=percentile(values, 50),
            p95_ms=percentile(values, 95),
            max_ms=max(values) if values else math.nan,
            budget_ms=BUDGET_P95_MS[stage],
            owner=STAGE_OWNER[stage],
        )

    def report(self, *, stages: tuple[Stage, ...] | None = None) -> list[StageStats]:
        order = stages or tuple(Stage)
        return [self.stats(s) for s in order if self._samples.get(s)]

    def breaches(self, *, voice_only: bool = True) -> list[StageStats]:
        """Stages whose p95 is over budget. This is what a load test asserts on."""
        rows = self.report(stages=VOICE_OWNED if voice_only else None)
        return [r for r in rows if r.within_budget is False]

    def unmeasured(self, *, voice_only: bool = True) -> list[Stage]:
        """Budgeted stages with no samples at all.

        Reported separately from breaches because "nothing was measured" and "everything
        passed" look identical otherwise, and the first one is how a broken benchmark
        announces success.
        """
        stages = VOICE_OWNED if voice_only else tuple(Stage)
        return [s for s in stages if BUDGET_P95_MS[s] is not None and not self._samples.get(s)]

    def format_report(self, *, voice_only: bool = False) -> str:
        rows = self.report(stages=VOICE_OWNED if voice_only else None)
        if not rows:
            return "no latency samples recorded"
        head = (
            f"{'stage':<22}{'n':>4}{'p50 ms':>10}{'p95 ms':>10}"
            f"{'max ms':>10}{'budget':>9}  status"
        )
        lines = [head, "-" * len(head)]
        for r in rows:
            budget = "-" if r.budget_ms is None else f"{r.budget_ms:.0f}"
            if r.within_budget is None:
                status = "no budget"
            elif r.within_budget:
                status = f"OK  (+{r.headroom_ms:.0f} ms headroom)"
            else:
                status = f"OVER by {-r.headroom_ms:.0f} ms"
            lines.append(
                f"{r.stage.value:<22}{r.count:>4}{r.p50_ms:>10.1f}{r.p95_ms:>10.1f}"
                f"{r.max_ms:>10.1f}{budget:>9}  {status}"
            )
        return "\n".join(lines)

    @property
    def turns(self) -> tuple[TurnLatency, ...]:
        return tuple(self._turns)


@dataclass
class EngineCounters:
    """Cheap counters that make a bad night debuggable after the fact."""

    frames_in: int = 0
    frame_gaps: int = 0
    wake_events: int = 0
    utterances: int = 0
    barge_ins: int = 0
    endpoint_by_silence: int = 0
    endpoint_by_max_duration: int = 0
    hesitation_extensions: int = 0
    tts_cancellations: int = 0
    extra: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, int]:
        base = {
            "frames_in": self.frames_in,
            "frame_gaps": self.frame_gaps,
            "wake_events": self.wake_events,
            "utterances": self.utterances,
            "barge_ins": self.barge_ins,
            "endpoint_by_silence": self.endpoint_by_silence,
            "endpoint_by_max_duration": self.endpoint_by_max_duration,
            "hesitation_extensions": self.hesitation_extensions,
            "tts_cancellations": self.tts_cancellations,
        }
        base.update(self.extra)
        return base


def statistics_summary(values: list[float]) -> str:  # small helper for eval scripts
    if not values:
        return "n=0"
    return (
        f"n={len(values)} mean={statistics.fmean(values):.1f} "
        f"p50={percentile(values, 50):.1f} p95={percentile(values, 95):.1f} max={max(values):.1f}"
    )
