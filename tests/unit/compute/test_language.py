"""Reply language, per utterance, including the code-switch case that motivates all of it."""

from __future__ import annotations

import pytest
from ars_compute.language import (
    DiacriticRepairStream,
    LanguageSource,
    detect_matrix_language,
    diacritics_report,
    resolve_reply_language,
    restore_diacritics,
)
from ars_protocol import Device, Language, Session
from helpers import utterance


# ---------------------------------------------------------------- straightforward cases
@pytest.mark.parametrize("text", [
    "What's on my calendar tomorrow morning?",
    "Can you summarise the last email from the bank?",
])
def test_english_stays_english(text: str) -> None:
    r = resolve_reply_language(utterance(text, Language.EN))
    assert r.language is Language.EN
    assert r.source is LanguageSource.TRANSCRIPT


@pytest.mark.parametrize("text", [
    "Ce am în calendar mâine dimineață?",
    "Poți să îmi rezumi ultimul e-mail de la bancă?",
])
def test_romanian_stays_romanian(text: str) -> None:
    r = resolve_reply_language(utterance(text, Language.RO))
    assert r.language is Language.RO


# ---------------------------------------------------------------- code switching
CODE_SWITCHED = [
    "Poți să faci un rebase pe branch-ul de staging?",
    "Mi-a picat build-ul, poți să te uiți în logs?",
    "Trimite-mi un link cu pull request-ul de ieri, te rog.",
    "Am nevoie de un review la commit-urile din sprint-ul asta.",
]


@pytest.mark.parametrize("text", CODE_SWITCHED)
def test_romanian_with_english_technical_terms_answers_in_romanian(text: str) -> None:
    """The whole point. ASR routinely tags these `en` because the content words are
    English; answering in English is the failure this module exists to prevent."""
    r = resolve_reply_language(utterance(text, Language.EN, confidence=0.83))
    assert r.language is Language.RO, f"{text!r} -> {r.language} via {r.source}"
    assert r.source is LanguageSource.MATRIX_OVERRIDE
    assert r.overrode_asr


@pytest.mark.parametrize("text", CODE_SWITCHED)
def test_borrowed_words_are_not_counted_as_english(text: str) -> None:
    ev = detect_matrix_language(text)
    assert ev.code_switched
    assert ev.borrowings, "expected the English technical terms to be recognised as borrowings"
    # Not one of them may contribute to the English score.
    assert not set(ev.en_hits) & set(ev.borrowings)


def test_a_single_english_noun_does_not_flip_the_language() -> None:
    ev = detect_matrix_language("Vreau să văd dashboard-ul.")
    assert ev.language is Language.RO
    assert ev.ro_score > ev.en_score


def test_genuinely_english_sentence_with_no_romanian_is_english() -> None:
    ev = detect_matrix_language("Can you rebase the staging branch for me?")
    assert ev.language is Language.EN


def test_session_pin_beats_everything() -> None:
    s = Session(device=Device.DESKTOP, preferred_language=Language.EN)
    r = resolve_reply_language(utterance("Ce faci?", Language.RO), session=s)
    assert r.language is Language.EN
    assert r.source is LanguageSource.SESSION_PIN


def test_low_asr_confidence_falls_back_to_the_text() -> None:
    r = resolve_reply_language(utterance("Îmi spui ce oră este?", Language.EN, confidence=0.4))
    assert r.language is Language.RO
    assert r.source is LanguageSource.LOW_ASR_CONFIDENCE


def test_typed_input_uses_the_text_not_the_client_default() -> None:
    r = resolve_reply_language(utterance("Care e programul meu de mâine?", Language.EN), typed=True)
    assert r.language is Language.RO
    assert r.source is LanguageSource.TEXT_INPUT


# ---------------------------------------------------------------- diacritics
def test_restore_diacritics_fixes_unambiguous_words() -> None:
    text = "Iti raspund maine dimineata, dupa ce inteleg intrebarea."
    out = restore_diacritics(text, Language.RO)
    assert out == "Îți răspund mâine dimineața, după ce înțeleg întrebarea."


def test_restore_diacritics_leaves_ambiguous_words_alone() -> None:
    # "sa" could be "să" or "sa"; "tine" could be "ține" or "tine". Guessing changes meaning.
    text = "Vreau sa tine cont de asta."
    assert restore_diacritics(text, Language.RO) == text


def test_restore_diacritics_never_touches_english() -> None:
    text = "I want to see the staging branch and the logs."
    assert restore_diacritics(text, Language.EN) == text


def test_restore_diacritics_leaves_code_and_paths_alone() -> None:
    text = "Fișierul e la src/ars_compute/context.py si ruleaza cu make test."
    out = restore_diacritics(text, Language.RO)
    assert "src/ars_compute/context.py" in out
    assert "make test" in out


def test_restore_diacritics_does_not_correct_borrowed_technical_terms() -> None:
    text = "Am facut un commit pe branch-ul de staging."
    out = restore_diacritics(text, Language.RO)
    assert "commit" in out and "branch-ul" in out and "staging" in out


def test_diacritics_report_scores_ascii_romanian_badly() -> None:
    bad = diacritics_report("Iti raspund maine dimineata.", Language.RO)
    good = diacritics_report("Îți răspund mâine dimineața.", Language.RO)
    assert bad.repairable and not bad.ok
    assert good.ok and good.score == 1.0
    assert good.score > bad.score


def test_streaming_repair_matches_batch_repair() -> None:
    """A word split across deltas must still come out right — this is the property that
    makes the repair usable in a streaming voice pipeline."""
    text = "Iti raspund maine dimineata, dupa ce inteleg intrebarea."
    stream = DiacriticRepairStream(Language.RO)
    out = "".join(stream.push(text[i:i + 3]) for i in range(0, len(text), 3)) + stream.flush()
    assert out == restore_diacritics(text, Language.RO)


def test_streaming_repair_is_a_passthrough_for_english() -> None:
    stream = DiacriticRepairStream(Language.EN)
    assert stream.push("hello ") == "hello "
    assert stream.push("world") == "world"
