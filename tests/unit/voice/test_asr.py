"""ASR: streaming contract (many partials, exactly one final) and bilingual behaviour.

The household switches EN <-> RO mid-conversation and must never have to announce it. The
counter-requirement is that a two-word utterance must not be allowed to flip the language of
the whole conversation, including the TTS voice.
"""

from __future__ import annotations

import pytest
from ars_protocol import SUPPORTED_LANGUAGES, Language
from ars_voice.asr.language import LanguageArbiter, constrain_probabilities, word_count
from ars_voice.asr.mock import BilingualScript, MockAsrEngine, ScriptedUtterance
from ars_voice.audio.frames import frames_from_pcm
from voice_helpers import speech, stream


async def transcribe(engine: MockAsrEngine, ms: float = 1_600, **kwargs):
    frames = frames_from_pcm(speech(ms))
    return [t async for t in engine.transcribe(stream(frames), **kwargs)]


async def test_partials_stream_then_exactly_one_final():
    engine = MockAsrEngine([ScriptedUtterance("turn the lights off", Language.EN, 0.95)])
    results = await transcribe(engine)
    finals = [t for t in results if t.is_final]
    partials = [t for t in results if not t.is_final]
    assert len(finals) == 1, "an utterance must end with exactly one final"
    assert partials, "no partials — the UI has no evidence it is being heard"
    assert results[-1].is_final
    assert [len(p.text) for p in partials] == sorted(len(p.text) for p in partials)
    assert finals[0].text == "turn the lights off"


async def test_an_empty_utterance_still_produces_a_final():
    """Otherwise a silent turn deadlocks a state machine that is waiting for is_final."""
    engine = MockAsrEngine([])
    results = await transcribe(engine, 400)
    assert len(results) == 1 and results[0].is_final and results[0].text == ""


@pytest.mark.parametrize(
    ("language", "text"),
    [
        (Language.EN, "send an email to Andrei about the invoice"),
        (Language.RO, "trimite un email lui Andrei despre factură"),
    ],
)
async def test_both_languages_round_trip(language: Language, text: str):
    engine = MockAsrEngine([ScriptedUtterance(text, language, 0.94)])
    results = await transcribe(engine)
    final = results[-1]
    assert final.is_final
    assert final.language is language
    assert final.text == text
    assert final.language_confidence > 0.9
    assert final.segments and final.segments[-1].end_ms > 0


async def test_the_user_switches_language_mid_conversation_without_announcing_it():
    script = BilingualScript(
        en=["what is on my calendar tomorrow morning"],
        ro=["ce am în calendar mâine dimineață"],
    ).utterances()
    engine = MockAsrEngine(script)
    first = (await transcribe(engine))[-1]
    second = (await transcribe(engine))[-1]
    assert first.language is Language.EN
    assert second.language is Language.RO, "a confident RO utterance must switch the language"


async def test_a_two_word_utterance_cannot_flip_the_conversation_language():
    """'ok' is not evidence of a language change. Flipping here would also change the voice
    A.R.S answers in, mid-conversation, for the rest of the session."""
    engine = MockAsrEngine(
        [
            ScriptedUtterance("adaugă lapte pe listă", Language.RO, 0.93),
            ScriptedUtterance("ok", Language.EN, 0.45),
        ]
    )
    first = (await transcribe(engine))[-1]
    second = (await transcribe(engine, 400))[-1]
    assert first.language is Language.RO
    assert second.language is Language.RO, "a 0.45-confidence single word flipped the language"
    assert second.language_confidence == pytest.approx(0.55)
    assert "refused switch" in engine.arbiter.history[-1].reason


def test_arbiter_switch_rules():
    arbiter = LanguageArbiter(
        current=Language.EN, switch_confidence=0.70,
        min_words_for_weak_switch=3, weak_switch_confidence=0.55,
    )
    # High confidence flips on any length.
    assert arbiter.decide(Language.RO, 0.88, text="da").switched
    arbiter.current = Language.EN
    # Weak confidence flips only with enough words.
    assert not arbiter.decide(Language.RO, 0.60, text="nu chiar").switched
    assert arbiter.decide(Language.RO, 0.60, text="nu chiar asta voiam").switched
    # Below the weak floor, never.
    arbiter.current = Language.EN
    assert not arbiter.decide(Language.RO, 0.40, text="a b c d e f").switched


def test_a_pinned_session_language_outranks_detection():
    arbiter = LanguageArbiter(current=Language.EN, pinned=Language.EN)
    decision = arbiter.decide(Language.RO, 0.99, text="ce faci în seara asta")
    assert decision.language is Language.EN
    assert not decision.switched
    assert "pins" in decision.reason


def test_detection_is_constrained_to_the_supported_languages():
    """Whisper reports Italian for Romanian audio often enough to matter. Constrain first,
    argmax second — otherwise 'auto-detect' picks a language with no voice and no prompts."""
    language, confidence = constrain_probabilities(
        {"it": 0.51, "ro": 0.30, "en": 0.10, "nl": 0.09}, SUPPORTED_LANGUAGES
    )
    assert language is Language.RO
    assert confidence == pytest.approx(0.75)


def test_constrain_handles_a_distribution_with_no_supported_language():
    language, confidence = constrain_probabilities({"it": 1.0}, SUPPORTED_LANGUAGES)
    assert language is Language.EN and confidence == 0.0


def test_word_count_handles_diacritics():
    assert word_count("adaugă pâine și lapte") == 4


async def test_an_explicit_language_bypasses_detection():
    engine = MockAsrEngine([ScriptedUtterance("da", Language.RO, 0.2)])
    final = (await transcribe(engine, 400, language=Language.RO))[-1]
    assert final.language is Language.RO
    assert final.language_confidence == 1.0


def test_supported_languages_come_from_the_protocol():
    assert MockAsrEngine().supported_languages == SUPPORTED_LANGUAGES
