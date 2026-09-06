"""End-to-end latency measurement on the mock path.

Runs whole turns — wakeword, endpointing, ASR, reply, TTS — over paced audio, and prints
p50/p95 for every stage against the budget in docs/architecture/overview.md.

Audio is replayed at wall-clock speed by default. That matters: without pacing you measure
how fast Python can iterate a list, not how long a person waits, and every number comes out
flatteringly close to zero.

What these numbers mean: everything except model compute. Wakeword scoring, ASR decode and
TTS synthesis are mocks, so their inference time is absent. The frame plumbing, the state
machine, the endpointing window, the scheduling, the queueing and the cancellation paths are
all real, and those are what this measures.
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
from ..fixtures import DEFAULT_DIR, load_specs
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


async def run_turns(turns: int, *, speed: float, root: Path | str) -> tuple[LatencyRecorder, list]:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Voice pipeline latency report (mock engines).")
    parser.add_argument("--turns", type=int, default=6)
    parser.add_argument(
        "--speed", type=float, default=1.0,
        help="audio replay speed; 1.0 is wall clock. Above 1.0 the endpointing row is "
             "compressed by the same factor and is no longer comparable to the budget.",
    )
    parser.add_argument("--root", default=str(DEFAULT_DIR))
    args = parser.parse_args(argv)

    recorder, states = asyncio.run(run_turns(args.turns, speed=args.speed, root=args.root))
    print(f"{args.turns} turns, audio replayed at {args.speed:.2f}x wall clock, mock engines\n")
    print(recorder.format_report())
    print()
    breaches = recorder.breaches()
    if breaches:
        print("OVER BUDGET (voice-owned rows):")
        for row in breaches:
            print(f"  {row.stage.value}: p95 {row.p95_ms:.0f} ms > {row.budget_ms:.0f} ms")
    else:
        print("all voice-owned rows within budget")
    print(f"\nstates observed: {' -> '.join(dict.fromkeys(s.value for s in states))}")
    print(
        "\nMock engines: wakeword/ASR/TTS inference time is NOT included. "
        "These are pipeline numbers, not model numbers."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
