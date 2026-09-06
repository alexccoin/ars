"""Wakeword evaluation: false accepts per hour and false reject rate.

A false accept means the microphone opened when nobody asked it to. That is a privacy
incident, not a quality metric, so the number is produced by counting detections over a
fixed set of audio and dividing by its duration — and it is written to
`research/benchmarks/wakeword/` so `WakewordEngine.false_accepts_per_hour` can *read* a
measurement instead of returning a guess.

An engine with no record here reports NaN. That is intended: NaN in a dashboard is a
question, and "we never measured it" is the honest answer to that question.

The generated fixture sets are SYNTHETIC. Any record produced from them is stamped as such
and is a check on this harness, not a statement about the user's household. Real FA/hour
needs hours of real ambient audio from the room A.R.S will live in.
"""

from __future__ import annotations

import argparse
import asyncio
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from ars_protocol import FRAME_MS, SAMPLE_RATE_HZ

from ..audio.frames import frames_from_pcm, pcm_to_float32
from ..audio.sources import read_wav
from ..audio.synth import KEYWORD_MARKER_HZ
from ..config import VoicePipelineConfig
from ..fixtures import DEFAULT_DIR, keyword_window_ms, load_specs
from ..wakeword.base import BufferedWakewordEngine
from ..wakeword.evaluation import WakewordEvaluation, format_evaluation, save_evaluation
from ..wakeword.mock import MockWakewordEngine


def marker_score(pcm: bytes) -> float:
    """Goertzel-style narrowband energy ratio at the synthetic marker frequency.

    A real detector for a fake keyword: it genuinely measures the audio, so the harness is
    exercising detection, refractory and pre-roll logic rather than a hardcoded answer.
    """
    samples = pcm_to_float32(pcm)
    if samples.size == 0:
        return 0.0
    total = float(np.sum(samples**2))
    if total <= 0:
        return 0.0
    n = samples.size
    k = 2 * np.pi * KEYWORD_MARKER_HZ * np.arange(n) / SAMPLE_RATE_HZ
    power = (float(np.dot(samples, np.cos(k))) ** 2 + float(np.dot(samples, np.sin(k))) ** 2)
    return min(1.0, (power / (n / 2)) / total)


@dataclass
class Detection:
    fixture: str
    at_ms: float
    score: float


async def _detections(engine: BufferedWakewordEngine, wav: Path) -> list[Detection]:
    frames = frames_from_pcm(read_wav(wav))

    async def stream():
        for frame in frames:
            yield frame

    out: list[Detection] = []
    async for event in engine.detect(stream()):
        # Position within the fixture: the engine's own elapsed-audio clock, not wall clock.
        out.append(Detection(fixture=wav.stem, at_ms=engine._elapsed_ms, score=event.score))
    return out


async def evaluate_engine(
    engine_factory,
    *,
    root: Path | str = DEFAULT_DIR,
    negative_set: str = "wakeword_negative",
    positive_set: str = "wakeword_positive",
) -> tuple[int, float, int, int, list[Detection]]:
    """Returns (false_accepts, negative_hours, positive_trials, false_rejects, detections)."""
    false_accepts = 0
    negative_ms = 0.0
    detections: list[Detection] = []
    for spec, wav in load_specs(negative_set, root):
        engine = engine_factory()
        hits = await _detections(engine, wav)
        detections.extend(hits)
        false_accepts += len(hits)
        negative_ms += spec.total_ms

    positive_trials = 0
    false_rejects = 0
    for spec, wav in load_specs(positive_set, root):
        engine = engine_factory()
        hits = await _detections(engine, wav)
        positive_trials += 1
        start, end = keyword_window_ms(spec)
        if not any(start <= hit.at_ms <= end for hit in hits):
            false_rejects += 1
    return false_accepts, negative_ms / 3_600_000.0, positive_trials, false_rejects, detections


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wakeword FA/hour and FR evaluation.")
    parser.add_argument("--root", default=str(DEFAULT_DIR))
    parser.add_argument("--engine", default="mock", choices=["mock", "mock-energy"])
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument(
        "--write", action="store_true",
        help="write the measured record to research/benchmarks/wakeword/",
    )
    args = parser.parse_args(argv)

    config = VoicePipelineConfig()
    threshold = args.threshold if args.threshold is not None else config.core.wakeword_threshold

    def factory():
        kwargs = dict(
            keyword=config.core.wakeword,
            threshold=threshold,
            pre_roll_ms=config.wakeword.pre_roll_ms,
            refractory_ms=config.wakeword.refractory_ms,
            benchmark_dir=config.wakeword.benchmark_dir,
        )
        if args.engine == "mock-energy":
            # Deliberately bad detector: fires on loudness alone. Kept as an option because
            # a harness that has never produced an alarming number has never been tested.
            return MockWakewordEngine(energy_trigger_db=-40.0, **kwargs)
        return MockWakewordEngine(score_fn=marker_score, **kwargs)

    fa, hours, trials, fr, detections = asyncio.run(
        evaluate_engine(factory, root=args.root)
    )
    record = WakewordEvaluation(
        engine="mock",
        keyword=config.core.wakeword,
        threshold=threshold,
        negative_audio_hours=hours,
        false_accepts=fa,
        positive_trials=trials,
        false_rejects=fr,
        negative_set=f"{args.root}/wakeword_negative (SYNTHETIC)",
        positive_set=f"{args.root}/wakeword_positive (SYNTHETIC)",
        notes=(
            "Synthetic fixtures generated by ars_voice.fixtures. Validates the evaluation "
            f"harness and the {args.engine} detector only. NOT a false-accept claim for any "
            "shipped wakeword: that needs hours of real ambient audio from the deployment room."
        ),
    )
    print(format_evaluation(record))
    print(f"frame size {FRAME_MS} ms, {len(detections)} detection(s) on the negative set")
    if args.write:
        if args.engine != "mock":
            print("refusing to write a record for a deliberately-bad detector")
            return 1
        path = save_evaluation(config.wakeword.benchmark_dir, record)
        print(f"wrote {path}")
    if math.isnan(record.false_accepts_per_hour):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
