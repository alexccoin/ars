#!/usr/bin/env python
"""First-token latency against a real Ollama. The source of the numbers in the README.

    ollama serve &
    uv run python research/benchmarks/compute/latency_live.py
    uv run python research/benchmarks/compute/latency_live.py --think   # the slow arm too

Separate from `run.py` on purpose. `run.py` measures behaviour with a scripted model and
must run anywhere, in CI, in a second. This measures a real model on real hardware, takes
a minute, and produces numbers that are only meaningful next to the machine they came from
— so it prints the machine.
"""

from __future__ import annotations

import argparse
import asyncio
import platform
import statistics
import time

from ars_compute import ReasoningMode
from ars_compute.backends.ollama import OllamaBackend
from ars_compute.context import ContextAssembler
from ars_compute.language import resolve_reply_language
from ars_protocol import Language, Transcript

FIRST_TOKEN_BUDGET_MS = 200  # docs/architecture/overview.md, "LLM first token"

PROMPTS: dict[Language, list[str]] = {
    Language.EN: [
        "What's on my calendar tomorrow?", "Did the bank send anything?",
        "Summarise my day.", "What time is it in Tokyo?", "Remind me to call Radu.",
        "How long is the flight to Rome?", "What's the weather like?",
        "Add milk to the shopping list.",
    ],
    Language.RO: [
        "Ce am în calendar mâine?", "Mi-a trimis banca ceva?", "Rezumă-mi ziua.",
        "Cât e ceasul în Tokyo?", "Amintește-mi să îl sun pe Radu.",
        "Cât durează zborul la Roma?", "Cum e vremea?",
        "Adaugă lapte pe lista de cumpărături.",
    ],
}

ASSEMBLER = ContextAssembler()


async def one(backend: OllamaBackend, text: str, language: Language,
              reasoning: ReasoningMode) -> tuple[float, float, int, int]:
    tr = Transcript(text=text, language=language, language_confidence=0.95, is_final=True)
    ctx = ASSEMBLER.assemble(turn_id="trn_" + "b" * 20,
                             language=resolve_reply_language(tr), transcript=tr)
    t0 = time.perf_counter()
    first = None
    async for piece in await backend.complete_context(ctx, reasoning=reasoning):
        if isinstance(piece, str) and piece.strip() and first is None:
            first = (time.perf_counter() - t0) * 1000
    total = (time.perf_counter() - t0) * 1000
    stats = backend.last_stats
    return (first or float("nan"), total,
            stats.input_tokens if stats else 0, stats.output_tokens if stats else 0)


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen3:14b")
    ap.add_argument("--host", default="http://127.0.0.1:11434")
    ap.add_argument("--think", action="store_true",
                    help="also measure the thinking arm (slow: minutes)")
    ap.add_argument("--no-warm", action="store_true",
                    help="skip prefix warming, to reproduce the cold-start cliff")
    args = ap.parse_args()

    backend = OllamaBackend(host=args.host, model=args.model)
    if not await backend.available():
        print(f"no Ollama at {args.host} — nothing to measure")
        return 2
    caps = await backend.capabilities()

    if not args.no_warm:
        timings = await backend.warm(
            [ASSEMBLER.build_system(lang)[0] for lang in PROMPTS]
        )
        warmed = ", ".join(f"{v:.0f} ms" for v in timings.values())
    else:
        warmed = "SKIPPED — expect a ~1.1 s first turn per language"

    print(f"\nmachine   {platform.platform()} / {platform.machine()}")
    print(f"model     {args.model}   capabilities: {sorted(caps) or 'unknown'}")
    print(f"warm      {warmed}")
    print(f"budget    llm_first_token p95 <= {FIRST_TOKEN_BUDGET_MS} ms\n")

    modes = [ReasoningMode.OFF] + ([ReasoningMode.ON] if args.think else [])
    print("| lang | think | first p50 | first p95 | total p50 | in tok | out tok | verdict |")
    print("|---|---|--:|--:|--:|--:|--:|:--|")
    failed = False
    for language, prompts in PROMPTS.items():
        for mode in modes:
            rows = [await one(backend, p, language, mode) for p in prompts]
            firsts = [r[0] for r in rows]
            p95 = pct(firsts, 0.95)
            ok = p95 <= FIRST_TOKEN_BUDGET_MS
            if mode is ReasoningMode.OFF and not ok:
                failed = True
            print(f"| {language.value} | {mode.value} | {statistics.median(firsts):.0f} ms "
                  f"| {p95:.0f} ms | {statistics.median([r[1] for r in rows]):.0f} ms "
                  f"| {statistics.median([r[2] for r in rows]):.0f} "
                  f"| {statistics.median([r[3] for r in rows]):.0f} "
                  f"| {'PASS' if ok else 'OVER BUDGET'} |")
    await backend.aclose()
    print()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
