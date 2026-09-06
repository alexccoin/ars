"""Measured wakeword quality — read from disk, never guessed.

`WakewordEngine.false_accepts_per_hour` exists because a false accept means the microphone
opened when nobody asked it to. That is a privacy incident, so the number must be an
*observation*, not a plausible-looking constant someone typed in during a refactor.

The contract of this module: if no evaluation record exists for an engine, callers get
NaN. NaN propagates and looks wrong in a dashboard, which is the correct outcome for an
engine that has never been measured. Nothing here ever invents a value.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

NOT_EVALUATED = math.nan
"""Sentinel for 'nobody has ever measured this'. Do not replace with 0.0."""


@dataclass(frozen=True)
class WakewordEvaluation:
    """One evaluation run on a fixed set. Serialised next to the model.

    `negative_audio_hours` and `false_accepts` are raw counts, so FA/hour is derived and
    auditable rather than stored as a single opaque float.
    """

    engine: str
    keyword: str
    threshold: float
    negative_audio_hours: float
    false_accepts: int
    positive_trials: int
    false_rejects: int
    negative_set: str
    positive_set: str
    evaluated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    notes: str = ""

    @property
    def false_accepts_per_hour(self) -> float:
        if self.negative_audio_hours <= 0:
            return NOT_EVALUATED
        return self.false_accepts / self.negative_audio_hours

    @property
    def false_accepts_per_hour_upper_95(self) -> float:
        """Upper 95% bound on FA/hour, given how much audio was actually listened to.

        Reported alongside the point estimate because "0 false accepts" over three minutes
        of audio is not evidence of anything. Zero counts use the rule of three (3/hours);
        non-zero counts use a normal approximation on the Poisson count. Both are crude and
        both are better than quoting a bare 0.000.
        """
        if self.negative_audio_hours <= 0:
            return NOT_EVALUATED
        if self.false_accepts == 0:
            return 3.0 / self.negative_audio_hours
        upper = self.false_accepts + 1.96 * math.sqrt(self.false_accepts)
        return upper / self.negative_audio_hours

    @property
    def underpowered(self) -> bool:
        """True when the negative set is too short to support a privacy claim."""
        return self.negative_audio_hours < 1.0

    @property
    def false_reject_rate(self) -> float:
        if self.positive_trials <= 0:
            return NOT_EVALUATED
        return self.false_rejects / self.positive_trials

    @property
    def is_synthetic(self) -> bool:
        """True when the sets are generated rather than recorded. A synthetic FA/hour is
        a plumbing check, not a privacy claim, and must be labelled as such wherever it
        is shown."""
        return "synthetic" in f"{self.negative_set} {self.positive_set} {self.notes}".lower()

    def to_json(self) -> str:
        payload = asdict(self)
        payload["derived"] = {
            "false_accepts_per_hour": self.false_accepts_per_hour,
            "false_accepts_per_hour_upper_95": self.false_accepts_per_hour_upper_95,
            "false_reject_rate": self.false_reject_rate,
            "underpowered": self.underpowered,
        }
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, data: dict) -> WakewordEvaluation:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def record_path(benchmark_dir: Path | str, engine: str, keyword: str) -> Path:
    return Path(benchmark_dir) / f"{engine}.{keyword}.json"


def load_evaluation(
    benchmark_dir: Path | str, engine: str, keyword: str
) -> WakewordEvaluation | None:
    """Returns None when the engine has never been evaluated. None is not an error here;
    silently substituting a number would be."""
    path = record_path(benchmark_dir, engine, keyword)
    if not path.is_file():
        return None
    try:
        return WakewordEvaluation.from_dict(json.loads(path.read_text()))
    except (json.JSONDecodeError, TypeError, ValueError):
        # A corrupt record is 'not evaluated', not 'evaluated as zero'.
        return None


def save_evaluation(benchmark_dir: Path | str, evaluation: WakewordEvaluation) -> Path:
    path = record_path(benchmark_dir, evaluation.engine, evaluation.keyword)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(evaluation.to_json())
    return path


class NotEvaluatedError(RuntimeError):
    """Raised by `assert_shippable()` on an engine with no evaluation record."""


def format_evaluation(evaluation: WakewordEvaluation | None) -> str:
    if evaluation is None:
        return "wakeword: NOT EVALUATED — FA/hour unknown, must not ship"
    label = " [SYNTHETIC SET]" if evaluation.is_synthetic else ""
    return (
        f"wakeword {evaluation.engine}/{evaluation.keyword} "
        f"@ thr={evaluation.threshold:.2f}{label}: "
        f"FA/h={evaluation.false_accepts_per_hour:.3f} "
        f"({evaluation.false_accepts} accepts / {evaluation.negative_audio_hours:.2f} h of "
        f"{evaluation.negative_set}), FR={evaluation.false_reject_rate:.3f} "
        f"({evaluation.false_rejects}/{evaluation.positive_trials} of {evaluation.positive_set}), "
        f"measured {evaluation.evaluated_at}"
    ) + (
        f"\n  WARNING: only {evaluation.negative_audio_hours * 60:.1f} min of negative audio — "
        f"FA/hour could be as high as {evaluation.false_accepts_per_hour_upper_95:.1f} at 95%. "
        "Not enough to make a privacy claim."
        if evaluation.underpowered
        else ""
    )
