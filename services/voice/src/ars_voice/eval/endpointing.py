"""Endpointing evaluation on a fixed fixture set.

Two numbers, and they trade against each other:

* **truncations** — the endpoint fired before the user finished. This is the one that makes
  A.R.S feel rude, and the target is zero. One truncation is worth more than 300 ms of
  latency on every other fixture.
* **endpoint latency** — endpoint time minus true speech end. This is the ENDPOINTING row
  of the budget table (p95 400 ms).

A configuration that improves latency while adding a truncation is a regression. Both
numbers, on the same fixture set, before and after: that is the rule.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from ars_protocol import FRAME_MS

from ..audio.frames import frames_from_pcm
from ..audio.sources import read_wav
from ..config import VoicePipelineConfig
from ..fixtures import DEFAULT_DIR, FixtureSpec, load_specs
from ..metrics import percentile
from ..vad.base import FrameVadEngine
from ..vad.endpointing import Endpointer, EndpointReason
from ..vad.energy import EnergyVadEngine


@dataclass
class FixtureResult:
    name: str
    language: str
    speech_end_ms: int
    endpoint_ms: float | None
    reason: str | None
    truncated: bool
    latency_ms: float | None

    @property
    def status(self) -> str:
        if self.endpoint_ms is None:
            return "NO ENDPOINT"
        if self.truncated:
            return f"TRUNCATED at {self.endpoint_ms:.0f} (speech ends {self.speech_end_ms})"
        return f"ok  +{self.latency_ms:.0f} ms"


@dataclass
class EvalResult:
    label: str
    results: list[FixtureResult]

    @property
    def truncations(self) -> int:
        return sum(1 for r in self.results if r.truncated)

    @property
    def missed(self) -> int:
        return sum(1 for r in self.results if r.endpoint_ms is None)

    @property
    def latencies(self) -> list[float]:
        return [r.latency_ms for r in self.results if r.latency_ms is not None and not r.truncated]

    def summary(self) -> str:
        lat = self.latencies
        return (
            f"{self.label:<34} truncations={self.truncations:<2} missed={self.missed:<2} "
            f"latency p50={percentile(lat, 50):6.0f} p95={percentile(lat, 95):6.0f} "
            f"max={max(lat) if lat else float('nan'):6.0f} ms"
        )


def run_fixture(
    spec: FixtureSpec, wav: Path, endpointer: Endpointer, vad: FrameVadEngine, *, use_partials: bool
) -> FixtureResult:
    """Replay one fixture through VAD + endpointing at frame granularity.

    `use_partials` feeds the endpointer the transcript a perfect ASR would have produced so
    far. That is the trailing-hesitation input; with it switched off you can measure exactly
    what hesitation handling is worth.
    """
    endpointer.reset()
    vad.reset()
    position = 0.0
    endpoint_ms: float | None = None
    reason: str | None = None
    for frame in frames_from_pcm(read_wav(wav)):
        position += FRAME_MS
        if use_partials:
            endpointer.note_partial(spec.transcript_at(position), spec.language)
        step = vad.step(frame)
        decision = endpointer.update(step.raw_is_speech, float(FRAME_MS))
        if decision.is_endpoint and decision.reason is not EndpointReason.NO_SPEECH:
            endpoint_ms = position
            reason = decision.reason.value if decision.reason else None
            break

    truncated = endpoint_ms is not None and endpoint_ms < spec.speech_end_ms
    latency = None if endpoint_ms is None else endpoint_ms - spec.speech_end_ms
    return FixtureResult(
        name=spec.name,
        language=spec.language.value,
        speech_end_ms=spec.speech_end_ms,
        endpoint_ms=endpoint_ms,
        reason=reason,
        truncated=truncated,
        latency_ms=latency,
    )


def evaluate(
    label: str,
    *,
    endpointer_factory,
    root: Path | str = DEFAULT_DIR,
    use_partials: bool = True,
    vad_factory=EnergyVadEngine,
) -> EvalResult:
    results = [
        run_fixture(spec, wav, endpointer_factory(), vad_factory(), use_partials=use_partials)
        for spec, wav in load_specs("endpointing", root)
    ]
    return EvalResult(label=label, results=results)


def default_endpointer(**overrides) -> Endpointer:
    config = VoicePipelineConfig()
    from ..vad.endpointing import endpointer_from_config

    endpointer = endpointer_from_config(config)
    for key, value in overrides.items():
        setattr(endpointer, key, value)
    return endpointer


def sweep(root: Path | str = DEFAULT_DIR) -> list[EvalResult]:
    """The comparison that justifies (or kills) any change to the defaults."""
    config = VoicePipelineConfig()
    base = float(config.core.endpoint_silence_ms)
    out: list[EvalResult] = []
    for silence in (300.0, 400.0, 500.0, 600.0, base, 900.0):
        out.append(
            evaluate(
                f"fixed silence {silence:.0f} ms",
                endpointer_factory=lambda s=silence: default_endpointer(
                    silence_ms=s, adaptive=False
                ),
                root=root,
            )
        )
    out.append(
        evaluate(
            "adaptive, no hesitation input",
            endpointer_factory=lambda: default_endpointer(adaptive=True),
            root=root,
            use_partials=False,
        )
    )
    out.append(
        evaluate(
            "adaptive + hesitation grace",
            endpointer_factory=lambda: default_endpointer(adaptive=True),
            root=root,
        )
    )
    out.append(
        evaluate(
            f"shipped default ({base:.0f} ms, adaptive off)",
            endpointer_factory=default_endpointer,
            root=root,
        )
    )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Endpointing evaluation on data/fixtures.")
    parser.add_argument("--root", default=str(DEFAULT_DIR))
    parser.add_argument("--sweep", action="store_true", help="compare configurations")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.sweep:
        results = sweep(args.root)
    else:
        results = [
            evaluate("shipped default", endpointer_factory=default_endpointer, root=args.root)
        ]

    if args.json:
        print(json.dumps([{"label": r.label, "truncations": r.truncations,
                           "missed": r.missed,
                           "p50_ms": percentile(r.latencies, 50),
                           "p95_ms": percentile(r.latencies, 95),
                           "fixtures": [vars(f) for f in r.results]} for r in results], indent=2))
        return 0

    for result in results:
        print(result.summary())
    print()
    print("per-fixture, shipped default:")
    for fixture in results[-1].results:
        print(f"  {fixture.name:<26} {fixture.language}  {fixture.status}")
    print()
    print("Fixtures are SYNTHETIC (see ars_voice.fixtures). Timing conclusions only.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
