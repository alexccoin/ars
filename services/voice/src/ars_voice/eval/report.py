"""End-to-end latency measurement against the budget in docs/architecture/overview.md
(as amended by docs/adr/0001-endpointing-latency-budget.md).

Two modes:

* `--engines mock` — the mock path. Bounds the plumbing: state machine, scheduling,
  queueing, the endpointing window. Runs anywhere, no weights.
* `--engines real` — the configured backends against real synthesised speech from
  `data/fixtures/spoken`. This is the number that decides whether A.R.S ships.

Audio is replayed at wall-clock speed by default. Without pacing you measure how fast Python
can iterate a list, not how long a person waits, and every stage comes out flatteringly near
zero.

Models are warmed before the clock starts, and the warm-up cost is reported separately. That
is not hiding it: a cold Piper voice is ~355 ms against a 120 ms budget, so warming is a
startup requirement (`VoicePipeline.warm_up`), and measuring it inside turn one would
describe a bug we have already fixed rather than the steady state the user lives in.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from ars_protocol import AgentState, Device, Language, Session

from ..asr.mock import MockAsrEngine, ScriptedUtterance
from ..audio.frames import frames_from_pcm
from ..audio.sinks import PacedSink
from ..audio.sources import frames_from_iterable, read_wav
from ..config import VoicePipelineConfig
from ..fixtures import DEFAULT_DIR, load_specs, load_spoken
from ..handler import EchoTurnHandler
from ..metrics import LatencyRecorder
from ..pipeline import VoicePipeline
from ..tts.mock import MockTtsEngine
from ..vad.energy import EnergyVadEngine
from ..wakeword.mock import MockWakewordEngine
from .wakeword import marker_score

TURN_SCRIPT = [
    ScriptedUtterance("turn the lights off in the kitchen", Language.EN, 0.94),
    ScriptedUtterance("stinge lumina din bucătărie", Language.RO, 0.91),
]


async def run_turns(
    turns: int, *, speed: float, root: Path | str
) -> tuple[LatencyRecorder, list]:
    """Mock path. Kept as the CI-safe measurement and the load test's fixture."""
    specs = load_specs("wakeword_positive", root)
    recorder = LatencyRecorder()
    states: list[AgentState] = []
    config = VoicePipelineConfig()

    for index in range(turns):
        _spec, wav = specs[index % len(specs)]
        pipeline = VoicePipeline(
            wakeword=MockWakewordEngine(
                score_fn=marker_score,
                keyword=config.core.wakeword,
                threshold=config.core.wakeword_threshold,
                pre_roll_ms=config.wakeword.pre_roll_ms,
            ),
            vad=EnergyVadEngine(),
            asr=MockAsrEngine([TURN_SCRIPT[index % len(TURN_SCRIPT)]]),
            tts=MockTtsEngine(chunk_ms=config.tts.chunk_ms),
            handler=EchoTurnHandler(),
            config=config,
            session=Session(device=Device.HEADLESS),
            sink=PacedSink(speed=max(speed, 1.0)),
            recorder=recorder,
        )
        frames = frames_from_pcm(read_wav(wav))
        async for event in pipeline.run(frames_from_iterable(frames, realtime=True, speed=speed)):
            if event.type == "state":
                states.append(event.state)
    return recorder, states


async def run_real_turns(
    turns: int, *, speed: float, root: Path | str
) -> tuple[LatencyRecorder, list[dict], dict[str, float]]:
    """The configured backends over real synthesised speech.

    One pipeline for all turns, warmed once — which is how a session actually runs. Building
    a fresh pipeline per turn would reload 1.6 GB of whisper weights between turns and report
    the cold path as if it were the steady state.
    """
    from ..factory import build_asr, build_tts, build_vad, build_wakeword

    fixtures = load_spoken(root)
    config = VoicePipelineConfig()
    recorder = LatencyRecorder()

    pipeline = VoicePipeline(
        wakeword=build_wakeword(config),
        vad=build_vad(config),
        asr=build_asr(config),
        tts=build_tts(config),
        handler=EchoTurnHandler(),
        config=config,
        session=Session(device=Device.HEADLESS),
        sink=PacedSink(speed=max(speed, 1.0)),
        recorder=recorder,
    )
    warm = await pipeline.warm_up()

    outcomes: list[dict] = []
    for index in range(turns):
        labels, wav = fixtures[index % len(fixtures)]
        frames = frames_from_pcm(read_wav(wav))
        finals = []
        async for event in pipeline.run(
            frames_from_iterable(frames, realtime=True, speed=speed)
        ):
            if event.type == "transcript" and event.transcript.is_final:
                finals.append(event.transcript)
        transcript = finals[-1] if finals else None
        outcomes.append(
            {
                "fixture": wav.stem,
                "expected_language": labels.get("expected_language"),
                "expected_text": labels.get("expected_text", ""),
                "text": transcript.text if transcript else "",
                "language": transcript.language.value if transcript else None,
                "confidence": transcript.language_confidence if transcript else 0.0,
            }
        )
    return recorder, outcomes, warm


def _describe(config: VoicePipelineConfig) -> str:
    return (
        f"wakeword={config.wakeword.backend}/{config.core.wakeword} "
        f"vad={config.vad.backend} asr={config.core.asr_backend}/{config.core.asr_model} "
        f"tts={config.tts.backend}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Voice pipeline latency report.")
    parser.add_argument("--turns", type=int, default=6)
    parser.add_argument(
        "--engines", choices=["mock", "real"], default="mock",
        help="'real' uses the backends selected by config and data/fixtures/spoken",
    )
    parser.add_argument(
        "--speed", type=float, default=1.0,
        help="audio replay speed; 1.0 is wall clock. Above 1.0 the endpointing row is "
             "compressed by the same factor and is no longer comparable to the budget.",
    )
    parser.add_argument("--root", default=str(DEFAULT_DIR))
    args = parser.parse_args(argv)

    config = VoicePipelineConfig()
    outcomes: list[dict] = []
    warm: dict[str, float] = {}
    if args.engines == "real":
        recorder, outcomes, warm = asyncio.run(
            run_real_turns(args.turns, speed=args.speed, root=args.root)
        )
        states = []
        print(f"{args.turns} turns, REAL engines: {_describe(config)}")
    else:
        recorder, states = asyncio.run(run_turns(args.turns, speed=args.speed, root=args.root))
        print(f"{args.turns} turns, mock engines")
    print(f"audio replayed at {args.speed:.2f}x wall clock\n")

    if warm:
        print("warm-up (once, at startup, off the hot path):")
        print("  " + "  ".join(f"{k}={v:.0f} ms" for k, v in warm.items()))
        print()

    print(recorder.format_report())
    print()
    breaches = recorder.breaches()
    missing = recorder.unmeasured()
    if breaches:
        print("OVER BUDGET (voice-owned rows):")
        for row in breaches:
            print(f"  {row.stage.value}: p95 {row.p95_ms:.0f} ms > {row.budget_ms:.0f} ms")
    if missing:
        print("NOT MEASURED (the run did not reach these stages):")
        for stage in missing:
            print(f"  {stage.value}")
    if not breaches and not missing:
        print("all voice-owned rows within budget")

    if outcomes:
        print("\ntranscripts (a fast wrong answer is not a pass):")
        for outcome in outcomes:
            ok = "ok " if outcome["language"] == outcome["expected_language"] else "LANG!"
            print(
                f"  {ok} {outcome['fixture']:<20} {outcome['language']} "
                f"conf={outcome['confidence']:.2f}  {outcome['text']!r}"
            )
    if states:
        print(f"\nstates observed: {' -> '.join(dict.fromkeys(s.value for s in states))}")
    if args.engines == "mock":
        print(
            "\nMock engines: wakeword/ASR/TTS inference time is NOT included. "
            "These are pipeline numbers, not model numbers."
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
