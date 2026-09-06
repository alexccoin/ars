#!/usr/bin/env python
"""Does translating a document at ingest make the document tier cross-lingual?

    ollama serve &
    uv run python research/benchmarks/translation/ingest_retrieval.py

`research/benchmarks/retrieval_calibration.py` measured the hole and
`ars_gateway.brain.TierConfig.document_threshold` documents it: a question asked in a
language the document is not written in scores 0.778-0.817, and irrelevant questions
reach 0.817 too. Nothing separates them, so tier 1 is same-language only. A bigger
embedding model was measured and does not fix it (-0.007 separation on e5-base).

This script tests the other repair. Instead of asking the embedding model to bridge the
languages, **bridge them at ingest**: translate the document once, index the translations
alongside the original, and let every question match a passage in its own language. The
cross-lingual problem becomes a same-language problem, which is the one the embedding
model is measured to be good at.

Three arms, same fixtures, same gate:

    A  ONE-COPY     German document indexed as-is             (what ships today)
    B  THREE-COPY   German + EN translation + RO translation  (translate the document)
    C  QUERY-SIDE   German only; the question is translated into German at query time

C is the arm that looks cheaper — no index growth, no ingest job — and it is the one to
check rather than assume, because it moves a translation onto the hot path of a tier
budgeted at ~20 ms.

It prints, per arm and per question language, the calibrated confidence the tier would
compute, whether the 0.85 gate fires, and whether the 0.04 ambiguity margin fires. The
margin is the part that is easy to get wrong and is the reason this script exists rather
than a paragraph of reasoning: three translations of the same paragraph are near-identical
vectors, so the runner-up to any hit is its own twin, and the margin rule that exists to
catch ambiguity would reject every single answer. Whether that happens is a measurement,
not an opinion.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services/gateway/src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ars_gateway.brain import TierConfig, confidence_from_cosine  # noqa: E402
from ars_gateway.documents import chunk, normalise  # noqa: E402
from ars_memory.embeddings.sentence_transformer_backend import (  # noqa: E402
    DEFAULT_MODEL,
    E5_PASSAGE_PREFIX,
    E5_QUERY_PREFIX,
    SentenceTransformerEmbeddingBackend,
)
from fixtures import GERMAN_DOC, GERMAN_DOC_QUESTIONS, IRRELEVANT_QUESTIONS  # noqa: E402
from quality import OllamaTranslator  # noqa: E402

CHUNK_SIZE = 1100
CONFIG = TierConfig()


@dataclass
class Passage:
    doc_language: str
    """The language of the copy this passage came from. Carried so a hit can be
    deduplicated against its own translations before the margin rule is applied."""
    origin: int
    """Index of the source chunk this passage is a copy of. Two passages with the same
    origin are the same paragraph in different languages, never two competing answers."""
    text: str


def build(copies: dict[str, str]) -> list[Passage]:
    """Chunk every language copy, keeping the correspondence between them.

    Chunking is done per copy rather than per chunk-then-translate, because a translator
    given a chunk boundary mid-clause produces a worse sentence than one given the
    paragraph. The i-th chunk of a translation is assumed to correspond to the i-th chunk
    of the original — true here, and true for any document short enough to translate whole.
    For a 200-page PDF this becomes an alignment problem and the answer is to translate
    chunk-by-chunk with the previous chunk as context; that is noted in
    research/translation.md, not solved here.
    """
    out: list[Passage] = []
    for lang, text in copies.items():
        for i, piece in enumerate(chunk(normalise(text), size=CHUNK_SIZE,
                                        overlap=min(180, CHUNK_SIZE // 4))):
            out.append(Passage(doc_language=lang, origin=i, text=piece))
    return out


@dataclass
class Hit:
    confidence: float
    cosine: float
    passage: Passage


def rank(matrix: np.ndarray, passages: list[Passage], qvec: np.ndarray) -> list[Hit]:
    sims = matrix @ qvec
    hits = [Hit(confidence_from_cosine(float(s)), float(s), p)
            for p, s in zip(passages, sims, strict=True)]
    hits.sort(key=lambda h: h.confidence, reverse=True)
    return hits


def gate(hits: list[Hit], *, dedupe_origin: bool) -> tuple[bool, str, Hit, float]:
    """Apply `TieredBrain.try_cheap_tiers`'s tier-1 rules to a ranked list.

    `dedupe_origin=True` is the change this experiment proposes: the runner-up used for
    the ambiguity margin must be a *different paragraph*, not the same paragraph in
    another language. Without it, indexing translations makes the tier answer nothing.
    """
    top = hits[0]
    if dedupe_origin:
        runner = next((h for h in hits[1:] if h.passage.origin != top.passage.origin), None)
    else:
        runner = hits[1] if len(hits) > 1 else None
    runner_conf = runner.confidence if runner else 0.0

    if top.confidence < CONFIG.document_threshold:
        return False, f"below threshold ({top.confidence:.0%} < 85%)", top, runner_conf
    if len(top.passage.text) < CONFIG.min_passage_chars:
        return False, "passage too short", top, runner_conf
    if runner_conf > 0 and top.confidence - runner_conf < CONFIG.document_margin:
        return False, (f"ambiguous: runner-up {runner_conf:.0%} within "
                       f"{CONFIG.document_margin:.0%}"), top, runner_conf
    return True, f"answered at {top.confidence:.0%}", top, runner_conf


async def measure_query_side(doc: str, backend: SentenceTransformerEmbeddingBackend,
                             translator, pivot: str = "de") -> dict:
    """Arm C: index one language, translate the incoming question into it.

    Costs nothing at ingest and nothing in index size, and costs a full translation on
    every question that reaches tier 1 — a tier whose entire justification is that it is
    ~20 ms and therefore cheaper than waking the model.
    """
    passages = [Passage(pivot, i, p) for i, p in enumerate(
        chunk(normalise(doc), size=CHUNK_SIZE, overlap=min(180, CHUNK_SIZE // 4)))]
    matrix = np.vstack(await backend.embed_many(
        [E5_PASSAGE_PREFIX + p.text for p in passages]))

    print(f"\n=== arm C (query-side translation into {pivot}) — "
          f"{len(passages)} passages ===")
    answered: dict[str, int] = {}
    asked: dict[str, int] = {}
    confidences: dict[str, list[float]] = {}
    latencies: list[float] = []
    for question, qlang in GERMAN_DOC_QUESTIONS:
        if qlang == pivot:
            translated, ms = question, 0.0
        else:
            translated, _first, ms = await translator.translate(question, qlang, pivot)
            latencies.append(ms)
        qvec = await backend.embed(E5_QUERY_PREFIX + translated)
        hits = rank(matrix, passages, qvec)
        fires, why, top, runner = gate(hits, dedupe_origin=True)
        asked[qlang] = asked.get(qlang, 0) + 1
        answered[qlang] = answered.get(qlang, 0) + int(fires)
        confidences.setdefault(qlang, []).append(top.confidence)
        print(f"  {qlang:>6}  {top.confidence:5.0%} {top.cosine:6.3f}  {ms:5.0f}ms  "
              f"{'YES' if fires else ' no':>5}  {question}  ->  {translated}")

    noise = []
    for question in IRRELEVANT_QUESTIONS:
        translated, _f, _ms = await translator.translate(question, "en", pivot)
        qvec = await backend.embed(E5_QUERY_PREFIX + translated)
        noise.append(rank(matrix, passages, qvec)[0].cosine)
    print(f"  query-translation latency: p50 {statistics.median(latencies):.0f} ms, "
          f"max {max(latencies):.0f} ms  (tier 1 is budgeted at ~20 ms)")
    return {"asked": asked, "answered": answered, "confidences": confidences,
            "noise_max": max(noise), "passages": len(passages),
            "query_ms_p50": statistics.median(latencies)}


async def measure_arm(name: str, copies: dict[str, str],
                      backend: SentenceTransformerEmbeddingBackend,
                      *, dedupe_origin: bool) -> dict:
    passages = build(copies)
    vectors = await backend.embed_many([E5_PASSAGE_PREFIX + p.text for p in passages])
    matrix = np.vstack(vectors)

    print(f"\n=== arm {name}: {sorted(copies)} — {len(passages)} passages, "
          f"dedupe_origin={dedupe_origin} ===")
    print(f"  {'q lang':>6}  {'conf':>6} {'cos':>6}  {'runner':>6}  {'hit':>4}  "
          f"{'fires':>5}  question")

    answered: dict[str, int] = {}
    asked: dict[str, int] = {}
    confidences: dict[str, list[float]] = {}
    for question, qlang in GERMAN_DOC_QUESTIONS:
        qvec = await backend.embed(E5_QUERY_PREFIX + question)
        hits = rank(matrix, passages, qvec)
        fires, why, top, runner = gate(hits, dedupe_origin=dedupe_origin)
        asked[qlang] = asked.get(qlang, 0) + 1
        answered[qlang] = answered.get(qlang, 0) + int(fires)
        confidences.setdefault(qlang, []).append(top.confidence)
        print(f"  {qlang:>6}  {top.confidence:5.0%} {top.cosine:6.3f}  {runner:5.0%}  "
              f"{top.passage.doc_language:>4}  {'YES' if fires else ' no':>5}  "
              f"{question}   [{why}]")

    print(f"  {'-' * 70}")
    noise = []
    for question in IRRELEVANT_QUESTIONS:
        qvec = await backend.embed(E5_QUERY_PREFIX + question)
        hits = rank(matrix, passages, qvec)
        fires, why, top, runner = gate(hits, dedupe_origin=dedupe_origin)
        noise.append(top.cosine)
        flag = "  LEAK" if fires else ""
        print(f"  {'noise':>6}  {top.confidence:5.0%} {top.cosine:6.3f}  {runner:5.0%}  "
              f"{top.passage.doc_language:>4}  {'YES' if fires else ' no':>5}  "
              f"{question}{flag}")

    return {"asked": asked, "answered": answered, "confidences": confidences,
            "noise_max": max(noise), "passages": len(passages)}


def verdict(a: dict, b: dict, c: dict) -> None:
    print("\n=== verdict ===")
    print(f"  {'q lang':>6}  {'A as-is':>9}  {'B doc-xlat':>11}  {'C query-xlat':>13}  "
          f"{'A conf':>7} {'B conf':>7} {'C conf':>7}")
    for lang in ("de", "en", "ro"):
        row = []
        for arm in (a, b, c):
            row.append(f"{arm['answered'].get(lang, 0)}/{arm['asked'].get(lang, 0)}")
        ma, mb, mc = (statistics.mean(arm["confidences"].get(lang, [0.0]))
                      for arm in (a, b, c))
        print(f"  {lang:>6}  {row[0]:>9}  {row[1]:>11}  {row[2]:>13}  "
              f"{ma:>6.0%} {mb:>6.0%} {mc:>6.0%}")
    print(f"  arm C adds {c['query_ms_p50']:.0f} ms to every tier-1 question; "
          f"arms A and B add nothing.")
    print(f"  irrelevant question, highest cosine: A {a['noise_max']:.3f}  "
          f"B {b['noise_max']:.3f}  C {c['noise_max']:.3f}")
    gained = sum(b["answered"].values()) - sum(a["answered"].values())
    total = sum(a["asked"].values())
    if b["noise_max"] > 0.83:
        print(f"  WARNING: the noise floor moved to {b['noise_max']:.3f}. Translating at "
              f"ingest triples the number of passages an irrelevant question can match "
              f"against, and the calibration constants must be re-fitted before shipping.")
    if gained > 0:
        print(f"  translate-at-ingest answers {gained} more of {total} questions "
              f"without touching the embedding model.")
    else:
        print(f"  translate-at-ingest gains nothing here ({gained:+d}). Do not build it.")


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="qwen3:14b")
    ap.add_argument("--embedding-model", default=DEFAULT_MODEL)
    ap.add_argument("--no-dedupe", action="store_true",
                    help="apply the margin rule as it is written today, without treating "
                         "a passage's own translations as the same paragraph")
    args = ap.parse_args(argv[1:])

    translator = OllamaTranslator(args.model, temperature=0.1)
    if not await translator.available():
        print("ollama is not reachable.")
        return 2

    print(f"embedding model: {args.embedding_model}")
    print(f"translator:      {translator.label}")
    print("\ntranslating the German lease once, at ingest...")
    en, _, en_ms = await translator.translate(GERMAN_DOC, "de", "en")
    ro, _, ro_ms = await translator.translate(GERMAN_DOC, "de", "ro")
    print(f"  de->en {en_ms:.0f} ms, de->ro {ro_ms:.0f} ms "
          f"({len(GERMAN_DOC)} source chars)")
    await translator.aclose()

    backend = SentenceTransformerEmbeddingBackend(args.embedding_model)
    a = await measure_arm("A (one copy, ships today)", {"de": GERMAN_DOC}, backend,
                          dedupe_origin=False)
    b = await measure_arm("B (three copies, proposed)",
                          {"de": GERMAN_DOC, "en": en, "ro": ro}, backend,
                          dedupe_origin=not args.no_dedupe)
    translator = OllamaTranslator(args.model, temperature=0.1)
    c = await measure_query_side(GERMAN_DOC, backend, translator)
    await translator.aclose()
    verdict(a, b, c)

    print("\n--- the English copy the model produced ---")
    print(en)
    print("\n--- the Romanian copy ---")
    print(ro)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv)))
