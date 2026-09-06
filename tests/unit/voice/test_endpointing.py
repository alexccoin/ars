"""Endpointing. The bias under test: waiting too long is a nuisance, cutting the user off
is a failure. Every assertion here is about which of the two happens."""

from __future__ import annotations

import pytest
from ars_protocol import FRAME_MS, Language
from ars_voice.config import VoicePipelineConfig
from ars_voice.vad.endpointing import (
    Endpointer,
    EndpointReason,
    EndpointState,
    endpointer_from_config,
)


def feed(endpointer: Endpointer, pattern: list[tuple[bool, float]]):
    """Feed (is_speech, duration_ms) runs; return (elapsed_at_endpoint, step)."""
    elapsed = 0.0
    for is_speech, duration in pattern:
        for _ in range(int(duration // FRAME_MS)):
            elapsed += FRAME_MS
            step = endpointer.update(is_speech, float(FRAME_MS))
            if step.is_endpoint:
                return elapsed, step
    return elapsed, None


def test_endpoint_waits_exactly_the_configured_silence():
    endpointer = Endpointer(silence_ms=700, speech_onset_ms=100, min_utterance_ms=320)
    elapsed, step = feed(endpointer, [(True, 1_000), (False, 2_000)])
    assert step is not None
    assert step.reason is EndpointReason.SILENCE
    silence_waited = elapsed - 1_000
    assert silence_waited == pytest.approx(700, abs=FRAME_MS)


def test_silence_window_comes_from_the_shared_config_not_a_literal():
    config = VoicePipelineConfig()
    endpointer = endpointer_from_config(config)
    assert endpointer.silence_ms == float(config.core.endpoint_silence_ms)
    assert config.core.endpoint_silence_ms == 700  # VoiceConfig default


@pytest.mark.parametrize("silence_ms", [400, 700, 1_000])
def test_endpoint_scales_with_the_setting(silence_ms: int):
    endpointer = Endpointer(silence_ms=silence_ms, speech_onset_ms=100, min_utterance_ms=320)
    elapsed, step = feed(endpointer, [(True, 900), (False, 3_000)])
    assert step is not None
    assert elapsed - 900 == pytest.approx(silence_ms, abs=FRAME_MS)


def test_a_pause_shorter_than_the_window_does_not_end_the_turn():
    """The 'send an email to Andrei <breath> about the invoice' case."""
    endpointer = Endpointer(silence_ms=700)
    elapsed, step = feed(
        endpointer, [(True, 900), (False, 400), (True, 900), (False, 2_000)]
    )
    assert step is not None
    assert elapsed > 900 + 400 + 900, "endpointed during the breath — the user was cut off"
    assert elapsed - 2_200 == pytest.approx(700, abs=FRAME_MS)


def test_minimum_utterance_guard_ignores_a_cough():
    """A 100 ms transient must not become an utterance. It resets to IDLE and keeps waiting."""
    endpointer = Endpointer(silence_ms=700, min_utterance_ms=320, speech_onset_ms=100)
    _elapsed, step = feed(endpointer, [(True, 120), (False, 1_000)])
    assert step is None
    assert endpointer.state is EndpointState.IDLE

    # ...and a real utterance afterwards still works.
    _elapsed, step = feed(endpointer, [(True, 800), (False, 2_000)])
    assert step is not None and step.reason is EndpointReason.SILENCE


def test_speech_onset_guard_ignores_a_single_loud_frame():
    endpointer = Endpointer(speech_onset_ms=100)
    endpointer.update(True, float(FRAME_MS))
    assert endpointer.state is EndpointState.IDLE


def test_a_single_noisy_frame_does_not_restart_the_silence_countdown():
    """Measured on data/fixtures: one -54 dB noise frame used to reset a 700 ms timer and
    cost 340 ms on a fixture whose room noise happened to peak once."""
    endpointer = Endpointer(silence_ms=700, resume_onset_ms=60)
    elapsed, step = feed(
        endpointer,
        [(True, 800), (False, 400), (True, float(FRAME_MS)), (False, 2_000)],
    )
    assert step is not None
    assert elapsed - 800 == pytest.approx(700, abs=2 * FRAME_MS)


def test_sustained_resumed_speech_does_restart_the_countdown():
    endpointer = Endpointer(silence_ms=700, resume_onset_ms=60)
    elapsed, step = feed(
        endpointer, [(True, 800), (False, 400), (True, 300), (False, 2_000)]
    )
    assert step is not None
    assert elapsed - 1_500 == pytest.approx(700, abs=2 * FRAME_MS)


@pytest.mark.parametrize(
    ("language", "text"),
    [
        (Language.EN, "remind me to call the plumber and"),
        (Language.RO, "adaugă pâine pe listă și"),
    ],
)
def test_trailing_hesitation_buys_the_user_more_time(language: Language, text: str):
    """Both languages. A feature that only waits patiently in English is unfinished."""
    endpointer = Endpointer(silence_ms=700, hesitation_grace_ms=400)
    endpointer.note_partial(text, language)
    elapsed, step = feed(endpointer, [(True, 900), (False, 3_000)])
    assert step is not None
    assert elapsed - 900 == pytest.approx(1_100, abs=FRAME_MS)


@pytest.mark.parametrize(
    ("language", "text"),
    [
        (Language.EN, "turn the lights off"),
        (Language.RO, "stinge lumina din bucătărie"),
    ],
)
def test_a_finished_sentence_gets_no_extra_grace(language: Language, text: str):
    endpointer = Endpointer(silence_ms=700, hesitation_grace_ms=400)
    endpointer.note_partial(text, language)
    elapsed, _step = feed(endpointer, [(True, 900), (False, 3_000)])
    assert elapsed - 900 == pytest.approx(700, abs=FRAME_MS)


def test_trailing_comma_counts_as_unfinished():
    endpointer = Endpointer(silence_ms=700, hesitation_grace_ms=400)
    endpointer.note_partial("first, add milk,", Language.EN)
    assert endpointer.required_silence_ms == pytest.approx(1_100)


def test_max_duration_stops_a_runaway_open_microphone():
    endpointer = Endpointer(silence_ms=700, max_utterance_ms=2_000)
    _, step = feed(endpointer, [(True, 5_000)])
    assert step is not None and step.reason is EndpointReason.MAX_DURATION


def test_no_speech_after_wake_abandons_instead_of_holding_the_microphone_open():
    endpointer = Endpointer(no_speech_timeout_ms=1_000)
    _, step = feed(endpointer, [(False, 2_000)])
    assert step is not None and step.reason is EndpointReason.NO_SPEECH
    assert endpointer.state is EndpointState.ABANDONED


def test_adaptive_shortens_only_for_a_substantial_finished_utterance():
    endpointer = Endpointer(silence_ms=700, adaptive=True, confident_silence_ms=420)
    endpointer.note_partial("turn the lights off in the kitchen", Language.EN)
    for _ in range(60):
        endpointer.update(True, float(FRAME_MS))
    assert endpointer.required_silence_ms == pytest.approx(420)

    hesitant = Endpointer(silence_ms=700, adaptive=True, confident_silence_ms=420)
    hesitant.note_partial("turn the lights off in the", Language.EN)
    for _ in range(60):
        hesitant.update(True, float(FRAME_MS))
    assert hesitant.required_silence_ms == pytest.approx(1_100)


def test_adaptive_is_off_by_default():
    """It truncates two of the twelve endpointing fixtures. See the README's tuning table."""
    assert VoicePipelineConfig().endpointing.adaptive is False
