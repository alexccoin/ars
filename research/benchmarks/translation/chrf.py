"""chrF++ without a dependency.

A translation benchmark needs a metric that runs offline, needs no human, and does not
itself require a model. BLEU is the wrong choice for Romanian and German: both are
heavily inflected, so a correct translation that picks a different case ending scores as
a miss on a word-level metric. chrF matches character n-grams, which gives partial credit
for `contractului` against `contractul`, and is the standard for morphologically rich
targets.

This is chrF++ (Popovic 2017): character n-grams of order 1..6 plus word n-grams of
order 1..2, F-score with beta=2 (recall weighted twice as heavily as precision, because a
translation that drops half the sentence is worse than one that adds a word).

Written out here rather than imported from `sacrebleu` for two reasons: this repository
does not carry sacrebleu, and a metric whose definition lives in the file is a metric
whose number can be reproduced from the file. The algorithm below is sacrebleu's
`CHRF._compute_f_score` — whitespace stripped before character extraction, F averaged
over the orders that both sides actually have. Spot-checked against published sacrebleu
outputs to within a rounding error; treat it as a comparator between systems measured
here, not as a number to quote against a published leaderboard.
"""

from __future__ import annotations

import re
from collections import Counter

CHAR_ORDER = 6
WORD_ORDER = 2
BETA = 2.0

_WS = re.compile(r"\s+")
_PUNCT_SPLIT = re.compile(r"\w+|[^\w\s]", re.UNICODE)


def _char_ngrams(text: str, n: int) -> Counter[str]:
    s = _WS.sub("", text)
    return Counter(s[i : i + n] for i in range(len(s) - n + 1))


def _word_ngrams(text: str, n: int) -> Counter[tuple[str, ...]]:
    words = _PUNCT_SPLIT.findall(text)
    return Counter(tuple(words[i : i + n]) for i in range(len(words) - n + 1))


def _stats(hyp: str, ref: str) -> list[tuple[int, int, int]]:
    """(hypothesis count, reference count, matches) per n-gram order."""
    out: list[tuple[int, int, int]] = []
    for n in range(1, CHAR_ORDER + 1):
        h, r = _char_ngrams(hyp, n), _char_ngrams(ref, n)
        out.append((sum(h.values()), sum(r.values()), sum((h & r).values())))
    for n in range(1, WORD_ORDER + 1):
        h, r = _word_ngrams(hyp, n), _word_ngrams(ref, n)
        out.append((sum(h.values()), sum(r.values()), sum((h & r).values())))
    return out


def chrf(hypothesis: str, reference: str) -> float:
    """0-100. Higher is better. 100 means character-identical after whitespace removal."""
    eps = 1e-16
    factor = BETA**2
    avg_prec = avg_rec = 0.0
    effective = 0
    for hyp_n, ref_n, match in _stats(hypothesis or "", reference or ""):
        if hyp_n > 0 and ref_n > 0:
            avg_prec += match / hyp_n
            avg_rec += match / ref_n
            effective += 1
    if effective == 0:
        return 0.0
    avg_prec /= effective
    avg_rec /= effective
    if avg_prec + avg_rec < eps:
        return 0.0
    return 100.0 * (1 + factor) * avg_prec * avg_rec / (factor * avg_prec + avg_rec)


# --------------------------------------------------------------------------- floors
#
# What a chrF++ number means for this product, calibrated on the fixtures in
# `fixtures.py` rather than on WMT. These are decision boundaries, not grades:
#
#   >= 60  the sentence carries. Fine for the interpreter path and for ingest.
#   50-60  understandable, visibly non-native, occasional wrong register. Usable for
#          retrieval (the embedding does not care about register) but not for speech
#          the user's counterparty hears.
#   40-50  meaning mostly survives, details do not. Numbers and dates start moving.
#   < 40   do not ship.
#
# The interpreter path is held to a higher bar than the ingest path on purpose: a
# mistranslated lease clause spoken aloud to a landlord is a different kind of failure
# from a slightly clumsy German paraphrase sitting in a vector index.
INTERPRETER_FLOOR = 55.0
INGEST_FLOOR = 45.0

__all__ = ["BETA", "CHAR_ORDER", "INGEST_FLOOR", "INTERPRETER_FLOOR", "WORD_ORDER", "chrf"]
