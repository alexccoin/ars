"""The confidence the arbiter reports for a language it KEPT.

This is the number that reaches `Transcript.language_confidence`, and downstream it
decides whether the text detector is allowed to overrule the acoustic one. Overstating it
makes A.R.S confidently answer in the wrong language.

It was overstated. The old code returned `1 - detected_confidence`, with a comment
explaining that the support set was binary so the complement was exactly the other
language's probability — true when written, false the moment German was added, and
silent, because a stale comment cannot fail a test. This is that test.
"""

from __future__ import annotations

import pytest
from ars_protocol import SUPPORTED_LANGUAGES, Language
from ars_voice.asr.language import LanguageArbiter, constrain_probabilities

NEAR_UNIFORM = {"en": 0.33, "ro": 0.33, "de": 0.34}


def test_constrain_returns_the_whole_distribution_not_just_the_winner() -> None:
    best, confidence, scores = constrain_probabilities(NEAR_UNIFORM)

    assert best is Language.DE
    assert confidence == pytest.approx(0.34, abs=0.01)
    assert set(scores) == set(SUPPORTED_LANGUAGES)
    assert sum(scores.values()) == pytest.approx(1.0)


def test_a_refused_switch_reports_the_kept_language_s_real_probability() -> None:
    """The failure this file exists for: three near-equal languages, a one-word utterance
    that is not evidence of anything, and a report of how sure we are in what we kept."""
    _best, confidence, scores = constrain_probabilities(NEAR_UNIFORM)
    arbiter = LanguageArbiter(current=Language.EN)

    decision = arbiter.decide(Language.DE, confidence, text="ja", scores=scores)

    assert decision.language is Language.EN
    assert not decision.switched
    assert decision.confidence == pytest.approx(scores[Language.EN], abs=1e-9)
    # The complement, 1 - 0.34 = 0.66, is the mass of *both* other languages. Reporting it
    # as English's alone was a two-fold overstatement.
    assert decision.confidence < 0.5


def test_without_a_distribution_the_estimate_spreads_the_remainder() -> None:
    """Some engines only hand back a label and a probability. Then the honest answer is an
    estimate — the remaining mass over the languages that were not detected — never the
    whole remainder attributed to one of them."""
    arbiter = LanguageArbiter(current=Language.EN)

    decision = arbiter.decide(Language.DE, 0.34, text="ja")

    others = len(SUPPORTED_LANGUAGES) - 1
    assert decision.confidence == pytest.approx((1.0 - 0.34) / others)
    assert decision.confidence < 1.0 - 0.34


def test_the_estimate_still_collapses_to_the_complement_for_two_languages() -> None:
    """The old arithmetic was not wrong, it was unguarded. With a binary support set the
    new code must agree with it exactly, or this is a behaviour change wearing a bug fix's
    clothes."""
    arbiter = LanguageArbiter(current=Language.EN, supported=(Language.EN, Language.RO))

    decision = arbiter.decide(Language.RO, 0.4, text="da")

    assert decision.confidence == pytest.approx(1.0 - 0.4)


def test_a_confident_detection_still_switches_and_reports_its_own_confidence() -> None:
    arbiter = LanguageArbiter(current=Language.EN)
    scores = {Language.EN: 0.05, Language.RO: 0.05, Language.DE: 0.90}

    decision = arbiter.decide(Language.DE, 0.90, text="wie viel kostet die Miete", scores=scores)

    assert decision.language is Language.DE
    assert decision.switched
    assert decision.confidence == pytest.approx(0.90)


def test_a_pinned_session_reports_the_pinned_language_s_probability() -> None:
    """Pinning outranks the model, but it must not fake certainty about it."""
    arbiter = LanguageArbiter(current=Language.EN, pinned=Language.RO)
    scores = {Language.EN: 0.10, Language.RO: 0.20, Language.DE: 0.70}

    decision = arbiter.decide(Language.DE, 0.70, text="wie viel", scores=scores)

    assert decision.language is Language.RO
    assert decision.confidence == pytest.approx(0.20)
