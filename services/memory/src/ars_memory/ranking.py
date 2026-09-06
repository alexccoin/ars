"""Hybrid recall ranking.

`SqliteMemoryStore.recall` gets two candidate sets — vector-similarity and
keyword/FTS5 — and merges them with a single documented formula:

    score = (W_VEC * vector_score) + (W_KW * keyword_score) + (W_RECENCY * recency_score)

Where, for a candidate record:

  * `vector_score` = max(0, cosine_similarity) between the query embedding and the
    record's embedding — 0 if the record was not returned by the vector search at all
    (i.e. it only matched on keywords).
  * `keyword_score` = a saturating transform of FTS5's `bm25()` rank:
    `raw / (raw + K)` where `raw = -bm25(memory_fts)` (FTS5's bm25 is *lower is better*
    and typically negative; negating and saturating turns it into a `[0, 1)` score,
    0 if the record was not returned by the FTS query at all (i.e. it only matched on
    the vector). `K` (`KEYWORD_SATURATION_K`) controls how fast the score approaches 1;
    it is a fixed constant, not tuned per query.
  * `recency_score` = `0.5 ** (age_days / HALF_LIFE_DAYS)` — exponential decay from
    `created_at_ms`, 1.0 for a brand new record, 0.5 after one half-life, etc.

Weights default to `W_VEC=0.55, W_KW=0.35, W_RECENCY=0.10` (see `MemoryConfig`) —
semantic similarity leads because it is what makes cross-lingual recall work at all
(shared keywords cannot bridge every EN/RO pair), keyword match is a strong secondary
signal for exact terms (names, numbers), and recency is a gentle tie-breaker, not a
dominant factor — an old durable fact ("I live in Cluj") must still outrank a brand new
irrelevant one.

Hard filters happen *before* this scoring, not as part of it:
  * `superseded_by IS NOT NULL` — never returned as a live memory (history is a
    separate, explicit query).
  * `valid_until_ms` in the past — expired facts are excluded, not merely downranked.
  * `language` filter, when the caller passed one to `recall(..., language=...)`.
"""

from __future__ import annotations

from dataclasses import dataclass

KEYWORD_SATURATION_K = 2.0
MS_PER_DAY = 86_400_000


@dataclass(frozen=True)
class RankWeights:
    vector: float = 0.55
    keyword: float = 0.35
    recency: float = 0.10


def keyword_score(bm25_raw: float | None) -> float:
    """`None` means the record did not match the FTS query at all."""
    if bm25_raw is None:
        return 0.0
    raw = max(0.0, -bm25_raw)
    return raw / (raw + KEYWORD_SATURATION_K)


def vector_score(cosine_similarity: float | None) -> float:
    if cosine_similarity is None:
        return 0.0
    return max(0.0, cosine_similarity)


def recency_score(age_ms: float, half_life_days: float) -> float:
    if half_life_days <= 0:
        return 0.0
    age_days = max(0.0, age_ms) / MS_PER_DAY
    return 0.5 ** (age_days / half_life_days)


def combine(
    *,
    vector_sim: float | None,
    bm25_raw: float | None,
    age_ms: float,
    weights: RankWeights,
    half_life_days: float,
) -> float:
    return (
        weights.vector * vector_score(vector_sim)
        + weights.keyword * keyword_score(bm25_raw)
        + weights.recency * recency_score(age_ms, half_life_days)
    )
