"""Unit tests for the hybrid ranking formula in isolation (no database)."""

from __future__ import annotations

from ars_memory import ranking


def test_vector_score_clamps_negative_similarity():
    assert ranking.vector_score(-0.5) == 0.0
    assert ranking.vector_score(None) == 0.0
    assert ranking.vector_score(0.8) == 0.8


def test_keyword_score_none_means_no_match():
    assert ranking.keyword_score(None) == 0.0


def test_keyword_score_increases_with_match_strength():
    weak = ranking.keyword_score(-0.5)
    strong = ranking.keyword_score(-5.0)
    assert 0.0 < weak < strong < 1.0


def test_recency_score_half_life():
    full = ranking.recency_score(0, half_life_days=90)
    half = ranking.recency_score(90 * ranking.MS_PER_DAY, half_life_days=90)
    assert full == 1.0
    assert abs(half - 0.5) < 1e-9


def test_combine_weights_sum_matters_more_than_any_single_signal():
    weights = ranking.RankWeights(vector=0.55, keyword=0.35, recency=0.10)
    best_vector = ranking.combine(
        vector_sim=1.0, bm25_raw=None, age_ms=365 * ranking.MS_PER_DAY, weights=weights,
        half_life_days=90,
    )
    best_keyword_only = ranking.combine(
        vector_sim=None, bm25_raw=-10.0, age_ms=0, weights=weights, half_life_days=90,
    )
    assert best_vector > 0
    assert best_keyword_only > 0
    # A strong vector match should still beat a keyword-only match at these weights.
    assert best_vector > best_keyword_only
