"""Re-fit the document tier's confidence calibration, in English and Romanian.

`brain.py` maps a raw cosine onto the "85% match" number Alex asked for, through a
logistic whose two constants come from measurement, not taste. This script is that
measurement. Run it whenever the embedding model, the chunk size, or the kind of
document A.R.S is expected to answer from changes:

    uv run python research/benchmarks/retrieval_calibration.py

It prints, per language and per chunk size:

  * the cosine a question scores against the passage that DOES answer it,
  * the cosine it scores against passages that do not,
  * the fitted CALIBRATION_MIDPOINT / CALIBRATION_STEEPNESS to paste into brain.py.

The bilingual part is not decoration. The same fixture set exists in EN and RO on the
same two topics, so a gap between the two is visible as a language effect rather than
hiding inside a topic effect — which is exactly the failure this script was written
after: RO questions scoring far below their EN twins and silently falling through to
the GPU.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services/gateway/src"))

from ars_gateway.documents import chunk, normalise  # noqa: E402
from ars_memory.embeddings.sentence_transformer_backend import (  # noqa: E402
    DEFAULT_MODEL,
    E5_PASSAGE_PREFIX,
    E5_QUERY_PREFIX,
    SentenceTransformerEmbeddingBackend,
)

CHUNK_SIZES = (1100, 500, 300)

# Two topics, each written in both languages, so language and topic are separable.
DOCS: dict[str, str] = {
    "rental.ro": """CONTRACT DE ÎNCHIRIERE — Strada Republicii 42, Cluj-Napoca

Chiria pe lună este de 4.200 lei, plătibilă până în data de 5 a fiecărei luni.
Garanția este de două chirii, adică 8.400 lei, returnabilă la predarea apartamentului.
Durata contractului este de 12 luni, începând cu 1 mai 2026.
Locul de parcare nu este inclus în chirie. Costă 300 lei pe lună în plus.
Utilitățile (curent, apă, gaz, internet) sunt plătite separat de chiriaș.
Rezilierea se face cu un preaviz de 60 de zile, notificat în scris.""",
    "rental.en": """LEASE AGREEMENT — 42 Republicii Street, Cluj-Napoca

The monthly rent is 4,200 lei, payable by the 5th day of each month.
The deposit is two months' rent, 8,400 lei, returned when the flat is handed back.
The term of the lease is 12 months, beginning 1 May 2026.
A parking space is not included in the rent. It costs an extra 300 lei per month.
Utilities (electricity, water, gas, internet) are paid separately by the tenant.
Termination requires 60 days' written notice.""",
    "employment.en": """EMPLOYMENT AGREEMENT — Senior Systems Engineer

Employment begins on 1 April 2026 at the Cluj-Napoca office.
The net monthly salary is 9,500 lei, paid on the last working day of each month.
The annual paid leave is 25 working days, plus the legal public holidays.
The probation period is 90 days. During probation either party may terminate with
5 working days' notice; after probation the notice period is 30 calendar days.
Remote work is allowed three days per week, subject to the manager's approval.""",
    "employment.ro": """CONTRACT INDIVIDUAL DE MUNCĂ — Inginer de sisteme senior

Angajarea începe la 1 aprilie 2026, la biroul din Cluj-Napoca.
Salariul net lunar este de 9.500 lei, plătit în ultima zi lucrătoare a lunii.
Concediul de odihnă anual este de 25 de zile lucrătoare, plus sărbătorile legale.
Perioada de probă este de 90 de zile. În perioada de probă contractul poate înceta cu
un preaviz de 5 zile lucrătoare; după probare preavizul este de 30 de zile calendaristice.
Munca de acasă este permisă trei zile pe săptămână, cu acordul managerului.""",
}

# (question, the document that answers it). Every question has a twin in the other
# language on the same topic, so EN and RO are compared like for like.
QUESTIONS: list[tuple[str, str]] = [
    ("How much is the monthly rent?", "rental.en"),
    ("Cât este chiria pe lună?", "rental.ro"),
    ("Is parking included in the rent?", "rental.en"),
    ("Locul de parcare este inclus în chirie?", "rental.ro"),
    ("How much is the deposit?", "rental.en"),
    ("Cât este garanția?", "rental.ro"),
    ("How much notice do I have to give to end the lease?", "rental.en"),
    ("Cu ce preaviz se reziliază contractul de închiriere?", "rental.ro"),
    ("What is the annual paid leave?", "employment.en"),
    ("Câte zile de concediu de odihnă am pe an?", "employment.ro"),
    ("What is the notice period after probation?", "employment.en"),
    ("Care este perioada de preaviz după probare?", "employment.ro"),
    ("What is the net monthly salary?", "employment.en"),
    ("Care este salariul net lunar?", "employment.ro"),
    ("How many days a week can I work from home?", "employment.en"),
    ("Câte zile pe săptămână pot lucra de acasă?", "employment.ro"),
]

# Questions no document answers. The threshold has to reject these, or the document tier
# becomes a machine for confident nonsense.
IRRELEVANT = [
    "What is the capital of Portugal?",
    "Care este capitala Portugaliei?",
    "how do I cook pasta?",
    "cum se gătește pastele?",
    "When does the next train to Bucharest leave?",
    "Ce vreme va fi mâine în Cluj?",
]


def language_of(question: str) -> str:
    return "ro" if any(c in question for c in "ăâîșțĂÂÎȘȚ") else "en"


async def measure(size: int, backend: SentenceTransformerEmbeddingBackend,
                  *, query_prefix: str = "", passage_prefix: str = "") -> dict:
    passages: list[tuple[str, str]] = []  # (doc key, chunk text)
    for key, text in DOCS.items():
        for piece in chunk(normalise(text), size=size, overlap=min(180, size // 4)):
            passages.append((key, piece))

    vectors = await backend.embed_many([passage_prefix + p for _, p in passages])
    matrix = np.vstack(vectors)

    rows = []
    for question, answering_doc in QUESTIONS:
        qvec = await backend.embed(query_prefix + question)
        sims = matrix @ qvec
        # Grouped by topic, not by file: `rental.ro` and `rental.en` are the same lease
        # in two languages, so an English question answered from the Romanian copy is a
        # *correct* answer, not a false match. Labelling it wrong here would have
        # manufactured an overlap that does not exist.
        topic = answering_doc.split(".")[0]
        relevant = max(
            float(s) for (key, _), s in zip(passages, sims, strict=True)
            if key.split(".")[0] == topic
        )
        other = max(
            float(s) for (key, _), s in zip(passages, sims, strict=True)
            if key.split(".")[0] != topic
        )
        rows.append((language_of(question), question, relevant, other))

    noise = []
    for question in IRRELEVANT:
        qvec = await backend.embed(query_prefix + question)
        noise.append((language_of(question), question, float(np.max(matrix @ qvec))))

    return {"rows": rows, "noise": noise, "passages": len(passages)}


def fit(relevant: list[float], irrelevant: list[float]) -> tuple[float, float, float]:
    """Place the logistic on the boundary between the two populations.

    The tier's job is a decision, so the fit is anchored on the decision boundary, not on
    the means: the midpoint (50% confidence) sits halfway between the weakest passage
    that DOES answer its question and the strongest one that does not, and the steepness
    is set so the weakest true match reads exactly 85%. The strongest false match then
    reads 15% by symmetry — which is the property worth checking, and the returned
    `separation` is what makes it possible. A separation <= 0 means no threshold can
    split them and the tier is unsafe with this model, whatever the constants say.
    """
    import math

    weakest_true = min(relevant)
    strongest_false = max(irrelevant)
    separation = weakest_true - strongest_false
    midpoint = (weakest_true + strongest_false) / 2
    # 0.85 = 1/(1+exp(-(weakest-mid)/k))  ->  k = (weakest-mid)/ln(0.85/0.15)
    steepness = (weakest_true - midpoint) / math.log(0.85 / 0.15)
    return midpoint, steepness, separation


# ------------------------------------------------------------------- cross-language
#
# The document tier's known limit, measured directly: the same question, asked in the
# language the document is NOT written in. This is the number that decides whether a
# bigger embedding model is worth a schema migration — cross-language relevant scores
# have to clear the noise floor, not merely beat chance.
CROSS_PROBES = [
    ("Cât este chiria pe lună?", "rental.ro", True, "same language"),
    ("How much is the monthly rent?", "rental.ro", True, "CROSS: EN question, RO lease"),
    ("Is parking included in the rent?", "rental.ro", True, "CROSS: EN question, RO lease"),
    ("What is the annual paid leave?", "employment.en", True, "same language"),
    ("Câte zile de concediu de odihnă am pe an?", "employment.en", True,
     "CROSS: RO question, EN contract"),
    ("Care este perioada de preaviz după probare?", "employment.en", True,
     "CROSS: RO question, EN contract"),
    ("What is the capital of Portugal?", "employment.en", False, "irrelevant"),
    ("Ce vreme va fi mâine în Cluj?", "rental.ro", False, "irrelevant"),
    ("cum se gătește pastele?", "employment.en", False, "irrelevant"),
]


async def measure_cross_language(backend: SentenceTransformerEmbeddingBackend,
                                 *, query_prefix: str, passage_prefix: str) -> None:
    keys = list(DOCS)
    vectors = dict(zip(keys, await backend.embed_many(
        [passage_prefix + normalise(DOCS[k]) for k in keys]
    ), strict=True))

    print("=== cross-language: asking in the language the document is not written in ===")
    cross, noise = [], []
    for question, doc, relevant, note in CROSS_PROBES:
        qvec = await backend.embed(query_prefix + question)
        cos = float(vectors[doc] @ qvec)
        if relevant and note.startswith("CROSS"):
            cross.append(cos)
        elif not relevant:
            noise.append(cos)
        print(f"  {cos:.3f}  {note:34s} {question}")
    gap = min(cross) - max(noise)
    if gap <= 0:
        print(f"  separation: {gap:+.3f}  -- cross-language questions cannot be told from "
              f"irrelevant ones. The tier stays same-language.\n")
    else:
        print(f"  separation: {gap:+.3f}  -- cross-language retrieval is usable with this "
              f"model.\n")


# --------------------------------------------------------------------------- tier 0
#
# The recall tier answers "is this the same question again?", which is a question↔question
# comparison and a different population from question↔passage above. It gets its own
# measurement and its own constant.
CACHED_ANSWERS = [
    "Q: Cât este chiria pe lună?\nA: Chiria pe lună este de 4.200 lei.",
    "Q: What is the annual paid leave?\nA: 25 working days, plus public holidays.",
]
RECALL_PROBES = [
    ("Cât este chiria pe lună?", 0, True, "identical"),
    ("cat este chiria pe luna", 0, True, "identical, no diacritics"),
    ("Care e chiria lunară?", 0, True, "reworded"),
    ("How much is the monthly rent?", 0, True, "asked in the other language"),
    ("Cât este garanția?", 0, False, "different question, same topic"),
    ("What is the annual paid leave?", 1, True, "identical"),
    ("How many holiday days do I get?", 1, True, "reworded"),
    ("What is the notice period?", 1, False, "different question, same topic"),
]


async def measure_recall(backend: SentenceTransformerEmbeddingBackend,
                         *, query_prefix: str, passage_prefix: str) -> None:
    cached = await backend.embed_many([passage_prefix + a for a in CACHED_ANSWERS])
    same, different = [], []
    print("=== tier 0: the same question, asked again ===")
    for question, idx, is_same, note in RECALL_PROBES:
        qvec = await backend.embed(query_prefix + question)
        cos = float(cached[idx] @ qvec)
        (same if is_same else different).append(cos)
        print(f"  {cos:.3f}  {'same' if is_same else 'DIFF'}  {note:32s} {question}")
    gap = min(same) - max(different)
    if gap <= 0:
        print(f"  separation: {gap:+.3f}  -- the recall tier cannot be gated safely.\n")
    else:
        print(f"  separation: {gap:+.3f}   recall_cosine anywhere in "
              f"({max(different):.3f}, {min(same):.3f})\n")


async def main(argv: list[str]) -> int:
    # e5-family models are asymmetric: they are trained with "query: " on the question
    # and "passage: " on the text, and score badly without them. Passed on the command
    # line so a candidate model can be measured before anything in the service changes.
    # No arguments measures what actually ships; arguments measure a candidate.
    model = argv[1] if len(argv) > 1 else DEFAULT_MODEL
    e5 = "e5" in model.lower()
    query_prefix = argv[2] if len(argv) > 2 else (E5_QUERY_PREFIX if e5 else "")
    passage_prefix = argv[3] if len(argv) > 3 else (E5_PASSAGE_PREFIX if e5 else "")

    backend = SentenceTransformerEmbeddingBackend(model)
    print(f"model: {model}  (query prefix {query_prefix!r}, passage prefix {passage_prefix!r})\n")

    for size in CHUNK_SIZES:
        result = await measure(size, backend, query_prefix=query_prefix,
                               passage_prefix=passage_prefix)
        rows = result["rows"]
        print(f"=== chunk size {size} ({result['passages']} passages) ===")
        for lang in ("en", "ro"):
            rel = [r for l, _, r, _ in rows if l == lang]
            oth = [o for l, _, _, o in rows if l == lang]
            print(f"  {lang}: answering passage  min {min(rel):.3f}  mean {statistics.mean(rel):.3f}"
                  f"  max {max(rel):.3f}")
            print(f"      other passages      max {max(oth):.3f}")
        worst = sorted(rows, key=lambda r: r[2])[:3]
        for lang, q, rel, oth in worst:
            print(f"      weakest [{lang}] {rel:.3f} (best other {oth:.3f})  {q}")
        noise_max = max(s for _, _, s in result["noise"])
        print(f"  irrelevant questions: max {noise_max:.3f}")

        rel_all = [r for _, _, r, _ in rows]
        irr_all = [s for _, _, s in result["noise"]] + [o for _, _, _, o in rows]
        mid, steep, sep = fit(rel_all, irr_all)
        if sep <= 0:
            print(f"  separation: {sep:+.3f}  -- NO THRESHOLD SEPARATES THESE. "
                  f"The document tier is unsafe with this model.\n")
        else:
            print(f"  separation: {sep:+.3f}   CALIBRATION_MIDPOINT = {mid:.4f}   "
                  f"CALIBRATION_STEEPNESS = {steep:.5f}\n")

    await measure_cross_language(backend, query_prefix=query_prefix,
                                 passage_prefix=passage_prefix)
    await measure_recall(backend, query_prefix=query_prefix, passage_prefix=passage_prefix)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv)))
