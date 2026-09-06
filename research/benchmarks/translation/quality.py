#!/usr/bin/env python
"""How good is a translator on EN/RO/DE, and how long does it take?

    ollama serve &
    uv run python research/benchmarks/translation/quality.py                 # qwen3:14b
    uv run python research/benchmarks/translation/quality.py --model qwen3:8b
    uv run python research/benchmarks/translation/quality.py --directions ro-de,de-ro

Sibling of `research/benchmarks/retrieval_calibration.py` and written to the same rule:
it measures, it prints the number, and it refuses to bless a configuration whose numbers
do not clear the floor. Three things come out per direction:

  * **chrF++** against the references in `fixtures.py`. Character n-grams, because RO and
    DE are inflected and a word-level metric punishes a correct translation for picking a
    different case ending. Comparator between systems, not a leaderboard score.
  * **kept**, the fraction of `must_keep` substrings that survived. Numbers, times, and
    borrowed technical nouns. This one needs no reference and can therefore be run on
    live traffic, which makes it the metric that would go into production telemetry.
  * **diacritics**, for Romanian targets only, scored by the *shipped* checker
    `ars_compute.language.diacritics_report`. A translator that produces "Chirie lunara"
    instead of "Chirie lunară" has produced text that Piper mispronounces, and reusing the
    production scorer here means the benchmark and the runtime cannot disagree.
  * **latency**, first token and total, warm. The interpreter path spends this budget
    twice per exchange (once per direction), so it is reported per sentence and the
    script converts it into the budget line from docs/architecture/overview.md.

The first call to each direction is discarded: it pays for the prompt prefix prefill, and
a startup cost measured as a per-turn cost is how a benchmark lies about a system that is
fine in practice.
"""

from __future__ import annotations

import argparse
import asyncio
import platform
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numerals  # noqa: E402
from chrf import INGEST_FLOOR, INTERPRETER_FLOOR, chrf  # noqa: E402
from fixtures import DIRECTIONS, LANGUAGE_NAMES, TRIPLES, Triple  # noqa: E402

# --------------------------------------------------------------------------- the prompt
#
# Candidate for `services/compute/src/ars_compute/prompts/translate.v1.md`. It lives here
# as a string ONLY because this file is a benchmark and the asset does not exist yet; the
# moment a TranslationEngine ships, this text moves into the prompt directory and this
# module loads it from there. CLAUDE.md rule on versioned prompts applies to the service,
# and the benchmark must measure the same bytes the service will send.
#
# Every line in it is here because of a specific observed failure:
#   * "Output only the translation" — qwen3 otherwise prefixes "Sure, here is the ..."
#     and the first token the user hears is English filler in a Romanian turn.
#   * the borrowed-term line — `ars_compute.language.TECHNICAL_BORROWINGS` exists because
#     this household says "branch-ul"; a translator that renders it as "ramura" has made
#     the sentence worse, not better.
#   * the diacritics line — `ars_compute.language.restore_diacritics` is a safety net for
#     a 4-bit model dropping them, and the net is much smaller if the prompt asks.
#   * the number line — a wrong figure in a lease clause is the failure that costs money.
SYSTEM = """You are a translation engine. You translate from {src_name} to {dst_name}.

Rules:
- Output only the translation. No preamble, no explanation, no quotation marks, no notes.
- Translate the meaning, not the words. Match the register of the source: an imperative
  stays an imperative, a contract clause stays formal.
- Keep every number, date, time, amount and proper name exactly as written in the source.
- Keep borrowed English technical terms in English (branch, staging, deploy, token, commit,
  pull request). Do not invent native equivalents for them.
- If the target language is Romanian, write correct Romanian with diacritics (ă â î ș ț).
- If the source is already in {dst_name}, return it unchanged.
"""

USER = "{text}"


# --------------------------------------------------------------------------- translators


@dataclass
class Result:
    triple_id: str
    domain: str
    src: str
    dst: str
    hypothesis: str
    reference: str
    chrf: float
    kept: tuple[bool, ...]
    diacritics: float
    numerals: numerals.NumeralCheck
    first_token_ms: float
    total_ms: float
    output_chars: int


class OllamaTranslator:
    """qwen3 (or any Ollama model) used as a translator.

    Goes through `ars_compute.backends.ollama.OllamaBackend` rather than talking to the
    HTTP API directly, because that class is the only sanctioned place Ollama's wire
    format is known (CLAUDE.md, rule 2) and because a benchmark that bypasses the seam
    measures something the product will never run.
    """

    def __init__(self, model: str, *, temperature: float = 0.1) -> None:
        from ars_compute.backends.ollama import OllamaBackend

        self.model = model
        self.backend = OllamaBackend(model=model, temperature=temperature, num_predict=256)
        self.label = f"ollama/{model} @ T={temperature}"

    async def available(self) -> bool:
        return await self.backend.available() and await self.backend.model_present()

    async def translate(self, text: str, src: str, dst: str) -> tuple[str, float, float]:
        from ars_compute.backends.base import Message
        from ars_compute.context import Role
        from ars_compute.reasoning import ReasoningMode
        from ars_protocol import Language

        system = SYSTEM.format(src_name=LANGUAGE_NAMES[src], dst_name=LANGUAGE_NAMES[dst])
        self.backend.reset()
        t0 = time.perf_counter()
        first: float | None = None
        out: list[str] = []
        async for piece in self.backend.stream(
            system=system,
            messages=[Message(role=Role.USER, text=USER.format(text=text))],
            tools=(),
            language=Language(dst),
            reasoning=ReasoningMode.OFF,
        ):
            if isinstance(piece, str) and piece:
                if first is None and piece.strip():
                    first = (time.perf_counter() - t0) * 1000
                out.append(piece)
        total = (time.perf_counter() - t0) * 1000
        return "".join(out).strip().strip('"'), (first if first is not None else total), total

    async def aclose(self) -> None:
        await self.backend.aclose()


class CtranslateTranslator:
    """A dedicated NMT model through CTranslate2 (Opus-MT, NLLB converted, MADLAD).

    Present so the comparison in `research/translation.md` can be re-run rather than
    argued about. Needs the converted model on disk under `models/mt/<name>`; it is not
    downloaded here, because CLAUDE.md forbids committing weights and a benchmark that
    silently pulls 3 GB is not a benchmark anyone runs twice.
    """

    def __init__(self, path: str, tokenizer: str, *, device: str = "cpu") -> None:
        import ctranslate2  # noqa: F401  (import here: vendor SDK, benchmark-local)
        from transformers import AutoTokenizer

        import ctranslate2 as ct2

        self.translator = ct2.Translator(path, device=device, compute_type="int8")
        self.tokenizer = AutoTokenizer.from_pretrained(tokenizer)
        self.label = f"ctranslate2/{Path(path).name} on {device}"

    async def available(self) -> bool:
        return True

    async def translate(self, text: str, src: str, dst: str) -> tuple[str, float, float]:
        t0 = time.perf_counter()
        tokens = self.tokenizer.convert_ids_to_tokens(self.tokenizer.encode(text))
        results = await asyncio.to_thread(
            self.translator.translate_batch, [tokens], beam_size=2, max_decoding_length=256
        )
        target = results[0].hypotheses[0]
        out = self.tokenizer.decode(self.tokenizer.convert_tokens_to_ids(target),
                                    skip_special_tokens=True)
        total = (time.perf_counter() - t0) * 1000
        # Not a streaming decoder: there is no first token before the last one. Reporting
        # total for both is the honest thing, and it is exactly the property that decides
        # whether this class of model can sit on the voice path at all.
        return out.strip(), total, total

    async def aclose(self) -> None:
        return None


# --------------------------------------------------------------------------- the run


def diacritics_score(text: str, dst: str) -> float:
    """Romanian only, and deliberately the production scorer rather than a local copy.

    `ars_compute.language.diacritics_report(...).score` is what the runtime already uses
    to decide whether a local model dropped its diacritics under load. A translation is
    just another string the same defect can appear in, so it is measured with the same
    ruler. German has no equivalent yet: umlauts are not systematically dropped by these
    models and there is no closed repair vocabulary to check against, so DE scores 1.0 and
    the gap is stated rather than faked.
    """
    if dst != "ro":
        return 1.0
    from ars_compute.language import diacritics_report
    from ars_protocol import Language

    return diacritics_report(text, Language.RO).score


async def run_direction(translator, src: str, dst: str, triples) -> list[Result]:
    results: list[Result] = []
    # Throwaway: pays the prompt-prefix prefill so it is not billed to sentence one.
    await translator.translate("Hello.", src, dst)
    for t in triples:
        hyp, first_ms, total_ms = await translator.translate(t.text(src), src, dst)
        ref = t.text(dst)
        results.append(Result(
            triple_id=t.id, domain=t.domain, src=src, dst=dst,
            hypothesis=hyp, reference=ref, chrf=chrf(hyp, ref),
            kept=tuple(k.lower() in hyp.lower() for k in t.must_keep),
            diacritics=diacritics_score(hyp, dst),
            numerals=numerals.check(t.text(src), hyp, src, dst),
            first_token_ms=first_ms, total_ms=total_ms, output_chars=len(hyp),
        ))
    return results


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


@dataclass
class DirectionSummary:
    src: str
    dst: str
    chrf_mean: float
    chrf_median: float
    chrf_min: float
    worst: Result
    kept_pct: float
    kept_total: int
    numerals_pct: float
    numerals_total: int
    diacritics_mean: float
    numeral_failures: tuple[Result, ...]
    p50_first: float
    p95_first: float
    p50_total: float
    p95_total: float
    by_domain: dict[str, float] = field(default_factory=dict)


def summarise(results: list[Result]) -> DirectionSummary:
    scores = [r.chrf for r in results]
    kept = [k for r in results for k in r.kept]
    num_total = sum(len(r.numerals.source_values) for r in results)
    num_kept = sum(r.numerals.kept for r in results)
    domains: dict[str, list[float]] = {}
    for r in results:
        domains.setdefault(r.domain, []).append(r.chrf)
    return DirectionSummary(
        src=results[0].src, dst=results[0].dst,
        chrf_mean=statistics.mean(scores), chrf_median=statistics.median(scores),
        chrf_min=min(scores), worst=min(results, key=lambda r: r.chrf),
        kept_pct=100.0 * sum(kept) / len(kept) if kept else float("nan"),
        kept_total=len(kept),
        numerals_pct=100.0 * num_kept / num_total if num_total else float("nan"),
        numerals_total=num_total,
        diacritics_mean=statistics.mean([r.diacritics for r in results]),
        numeral_failures=tuple(r for r in results if not r.numerals.ok),
        p50_first=percentile([r.first_token_ms for r in results], 0.50),
        p95_first=percentile([r.first_token_ms for r in results], 0.95),
        p50_total=percentile([r.total_ms for r in results], 0.50),
        p95_total=percentile([r.total_ms for r in results], 0.95),
        by_domain={d: statistics.mean(v) for d, v in sorted(domains.items())},
    )


def report(summaries: list[DirectionSummary], label: str, *, verbose_worst: bool) -> int:
    print(f"\n=== {label} — {len(TRIPLES)} sentences per direction ===")
    print(f"{'dir':>7}  {'chrF++':>7} {'med':>6} {'min':>6}  {'terms':>6} {'nums':>6}  "
          f"{'p50 1st':>8} {'p95 1st':>8}  {'p50 tot':>8} {'p95 tot':>8}")
    for s in summaries:
        kept = "  n/a " if s.kept_total == 0 else f"{s.kept_pct:5.0f}%"
        nums = "  n/a " if s.numerals_total == 0 else f"{s.numerals_pct:5.0f}%"
        diac = "  n/a " if s.dst != "ro" else f"{100 * s.diacritics_mean:5.1f}"
        print(f"{s.src}->{s.dst:>3}  {s.chrf_mean:7.1f} {s.chrf_median:6.1f} {s.chrf_min:6.1f}  "
              f"{kept} {nums} {diac}  {s.p50_first:7.0f}m {s.p95_first:7.0f}m  "
              f"{s.p50_total:7.0f}m {s.p95_total:7.0f}m")

    print("\n  by domain (mean chrF++):")
    domains = sorted({d for s in summaries for d in s.by_domain})
    print("           " + "".join(f"{d:>12}" for d in domains))
    for s in summaries:
        print(f"  {s.src}->{s.dst:<5}" + "".join(
            f"{s.by_domain.get(d, float('nan')):12.1f}" for d in domains))

    if verbose_worst:
        print("\n  worst sentence per direction:")
        for s in summaries:
            print(f"  [{s.src}->{s.dst}] {s.chrf_min:.1f}  {s.worst.triple_id}")
            print(f"      hyp: {s.worst.hypothesis}")
            print(f"      ref: {s.worst.reference}")

    print()
    failed = 0
    for s in summaries:
        if s.chrf_mean < INGEST_FLOOR:
            print(f"  FAIL  {s.src}->{s.dst} at {s.chrf_mean:.1f} is below the ingest floor "
                  f"({INGEST_FLOOR}). Do not use this model for this direction.")
            failed += 1
        elif s.chrf_mean < INTERPRETER_FLOOR:
            print(f"  WARN  {s.src}->{s.dst} at {s.chrf_mean:.1f} clears ingest "
                  f"({INGEST_FLOOR}) but not the interpreter floor ({INTERPRETER_FLOOR}).")
        if s.kept_total and s.kept_pct < 100.0:
            print(f"  FAIL  {s.src}->{s.dst} did not keep a borrowed technical term "
                  f"({s.kept_pct:.0f}% kept).")
            failed += 1
        if s.dst == "ro" and s.diacritics_mean < 0.98:
            print(f"  WARN  {s.src}->{s.dst} Romanian diacritics score "
                  f"{s.diacritics_mean:.3f}. Piper will mispronounce this; "
                  f"ars_compute.language.restore_diacritics repairs part of it.")
        if s.numerals_total and s.numerals_pct < 100.0:
            print(f"  FAIL  {s.src}->{s.dst} lost a numeric VALUE "
                  f"({s.numerals_pct:.0f}% of {s.numerals_total} kept). "
                  f"That is the failure that costs money.")
            for r in s.numeral_failures:
                print(f"          {r.triple_id}: missing {r.numerals.missing} "
                      f"from {r.numerals.output_values}")
                print(f"          hyp: {r.hypothesis}")
            failed += 1
    if not failed:
        print("  all directions clear the ingest floor.")
    return failed


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="qwen3:14b")
    ap.add_argument("--temperature", type=float, default=0.1)
    ap.add_argument("--directions", default="",
                    help="comma-separated, e.g. ro-de,de-ro. Default: all six.")
    ap.add_argument("--domains", default="", help="comma-separated domain filter")
    ap.add_argument("--ct2", default="", help="path to a CTranslate2 model directory")
    ap.add_argument("--ct2-tokenizer", default="")
    ap.add_argument("--ct2-device", default="cpu")
    ap.add_argument("--worst", action="store_true", help="print the worst sentence per pair")
    args = ap.parse_args(argv[1:])

    directions = DIRECTIONS
    if args.directions:
        directions = tuple(tuple(d.split("-")) for d in args.directions.split(","))  # type: ignore[misc]
    triples: tuple[Triple, ...] = TRIPLES
    if args.domains:
        wanted = {d.strip().upper() for d in args.domains.split(",")}
        triples = tuple(t for t in TRIPLES if t.domain in wanted)

    if args.ct2:
        translator = CtranslateTranslator(args.ct2, args.ct2_tokenizer or args.ct2,
                                          device=args.ct2_device)
    else:
        translator = OllamaTranslator(args.model, temperature=args.temperature)

    if not await translator.available():
        print(f"{translator.label} is not reachable. Start `ollama serve` and pull the model.")
        return 2

    print(f"machine: {platform.platform()} / {platform.processor()}")
    print(f"translator: {translator.label}")

    summaries = []
    for src, dst in directions:
        results = await run_direction(translator, src, dst, triples)
        summaries.append(summarise(results))
    failed = report(summaries, translator.label, verbose_worst=args.worst)
    await translator.aclose()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv)))
