"""Wakeword: pre-roll retention, refractory, and honest FA/hour reporting."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from ars_protocol import FRAME_MS, WakeEvent
from ars_voice.audio.frames import (
    BYTES_PER_FRAME,
    frames_from_pcm,
    pcm_from_frames,
    total_duration_ms,
)
from ars_voice.eval.wakeword import marker_score
from ars_voice.wakeword.base import BufferedWakewordEngine
from ars_voice.wakeword.evaluation import (
    NotEvaluatedError,
    WakewordEvaluation,
    load_evaluation,
    save_evaluation,
)
from ars_voice.wakeword.mock import MockWakewordEngine, NullWakewordEngine
from ars_voice.wakeword.prefix import spoken_forms, strip_wakeword_prefix
from voice_helpers import keyword, silence, speech, stream


async def detect_all(engine: BufferedWakewordEngine, frames) -> list[WakeEvent]:
    return [event async for event in engine.detect(stream(frames))]


async def test_pre_roll_retains_audio_from_before_detection():
    """The whole point of the ring buffer: the syllable before the wakeword fires survives.

    The audio is built so that the 500 ms preceding detection is a distinct, recognisable
    byte pattern; if the engine handed back audio captured after detection instead, or
    nothing at all, the assertion on the returned PCM fails.
    """
    before = silence(400, seed=1) + speech(600, seed=2)
    frames = frames_from_pcm(before)
    trigger_seq = len(frames) + 4
    after = speech(400, seed=3)
    frames += frames_from_pcm(after, start_seq=len(frames))

    engine = MockWakewordEngine(trigger_seqs={trigger_seq}, pre_roll_ms=500)
    events = await detect_all(engine, frames)

    assert len(events) == 1
    event = events[0]
    assert event.pre_roll_ms == 500

    pre_roll = engine.pre_roll_frames(event)
    assert pre_roll, "no pre-roll returned — the first syllable would be clipped"
    assert total_duration_ms(pre_roll) == pytest.approx(500, abs=FRAME_MS)

    # The retained audio must be the audio immediately *preceding* the detection window.
    detection_end = (trigger_seq // 4 + 1) * 4  # 80 ms inference window, 20 ms frames
    expected = pcm_from_frames(frames[detection_end - 25 : detection_end])
    assert pcm_from_frames(pre_roll) == expected


async def test_pre_roll_is_short_but_present_when_the_stream_just_opened():
    """Asking for 500 ms of pre-roll 240 ms after the mic opened returns 240 ms, not padding.

    Silently padding with zeros would put a fake leading silence in front of the utterance
    and quietly change what ASR hears at the start of every session."""
    frames = frames_from_pcm(speech(240))  # 12 frames = three whole 80 ms windows
    engine = MockWakewordEngine(trigger_seqs={len(frames) - 1}, pre_roll_ms=500)
    events = await detect_all(engine, frames)
    assert events
    assert events[0].pre_roll_ms < 500
    assert total_duration_ms(engine.pre_roll_frames(events[0])) == pytest.approx(
        events[0].pre_roll_ms, abs=FRAME_MS
    )


async def test_refractory_period_collapses_one_keyword_into_one_event():
    """A keyword crosses threshold on several consecutive windows. Users say it once."""
    pcm = silence(200) + keyword(600) + speech(600)
    frames = frames_from_pcm(pcm)
    engine = MockWakewordEngine(score_fn=marker_score, threshold=0.6, refractory_ms=1_500)
    events = await detect_all(engine, frames)
    assert len(events) == 1

    engine_no_refractory = MockWakewordEngine(
        score_fn=marker_score, threshold=0.6, refractory_ms=0
    )
    assert len(await detect_all(engine_no_refractory, frames)) > 1


async def test_frame_gap_is_counted_not_ignored():
    frames = frames_from_pcm(speech(400))
    del frames[5]
    engine = MockWakewordEngine(trigger_seqs=set())
    await detect_all(engine, frames)
    assert engine.counters.frame_gaps == 1


def test_false_accepts_per_hour_is_nan_when_never_evaluated(tmp_path: Path):
    engine = MockWakewordEngine(keyword="hey_ars", benchmark_dir=tmp_path)
    assert math.isnan(engine.false_accepts_per_hour)
    assert math.isnan(engine.false_reject_rate)
    with pytest.raises(NotEvaluatedError):
        engine.assert_shippable()


def test_false_accepts_per_hour_reads_the_measured_record(tmp_path: Path):
    save_evaluation(
        tmp_path,
        WakewordEvaluation(
            engine="mock", keyword="hey_ars", threshold=0.6,
            negative_audio_hours=4.0, false_accepts=2, positive_trials=50, false_rejects=1,
            negative_set="synthetic", positive_set="synthetic",
        ),
    )
    engine = MockWakewordEngine(keyword="hey_ars", threshold=0.6, benchmark_dir=tmp_path)
    assert engine.false_accepts_per_hour == pytest.approx(0.5)
    assert engine.false_reject_rate == pytest.approx(0.02)
    engine.assert_shippable(max_fa_per_hour=0.5)
    with pytest.raises(NotEvaluatedError, match="exceeds limit"):
        engine.assert_shippable(max_fa_per_hour=0.1)


def test_a_record_measured_at_another_threshold_is_not_reported(tmp_path: Path):
    """FA/hour is a function of the operating point. Quoting a number from a different
    threshold is worse than quoting nothing."""
    save_evaluation(
        tmp_path,
        WakewordEvaluation(
            engine="mock", keyword="hey_ars", threshold=0.9,
            negative_audio_hours=4.0, false_accepts=0, positive_trials=50, false_rejects=2,
            negative_set="synthetic", positive_set="synthetic",
        ),
    )
    engine = MockWakewordEngine(keyword="hey_ars", threshold=0.6, benchmark_dir=tmp_path)
    assert math.isnan(engine.false_accepts_per_hour)


def test_corrupt_record_reads_as_not_evaluated(tmp_path: Path):
    (tmp_path / "mock.hey_ars.json").write_text("{ this is not json")
    assert load_evaluation(tmp_path, "mock", "hey_ars") is None


def test_underpowered_negative_set_is_flagged():
    record = WakewordEvaluation(
        engine="mock", keyword="hey_ars", threshold=0.6,
        negative_audio_hours=0.03, false_accepts=0, positive_trials=10, false_rejects=0,
        negative_set="synthetic", positive_set="synthetic",
    )
    assert record.false_accepts_per_hour == 0.0
    assert record.underpowered
    assert record.false_accepts_per_hour_upper_95 == pytest.approx(100.0)


def test_null_engine_reports_a_real_zero():
    """The one engine allowed to claim 0.0: it cannot fire."""
    assert NullWakewordEngine().false_accepts_per_hour == 0.0


async def test_null_engine_never_fires():
    engine = NullWakewordEngine()
    assert await detect_all(engine, frames_from_pcm(keyword(600) + speech(600))) == []


def test_inference_window_must_be_a_multiple_of_the_protocol_frame():
    with pytest.raises(ValueError, match="FRAME_MS"):
        MockWakewordEngine(inference_window_ms=FRAME_MS * 2 + 1)
    assert BYTES_PER_FRAME > 0


# --------------------------------------------------------------------------- wake phrase

def test_spoken_forms_covers_the_carrier_word_and_the_name_alone():
    """openWakeWord fires at the *end* of the keyword, so a 500 ms pre-roll often catches only
    "Jarvis" — the transcript has to be cleaned of both spellings, longest first."""
    assert spoken_forms("hey_jarvis") == ("hey jarvis", "jarvis")
    assert spoken_forms("alexa") == ("alexa",)
    assert spoken_forms("hey_ars") == ("hey ars", "ars")
    assert spoken_forms("") == ()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The real transcript this was written for, measured on data/fixtures/spoken.
        (
            "Jarvis, good morning, I found three new messages from the bank.",
            "Good morning, I found three new messages from the bank.",
        ),
        ("Hey Jarvis, turn the lights off.", "Turn the lights off."),
        ("hey jarvis stinge lumina din bucătărie", "Stinge lumina din bucătărie"),
        ("Jarvis Bună dimineața!", "Bună dimineața!"),
    ],
)
def test_the_wake_phrase_is_stripped_from_the_front(text: str, expected: str):
    assert strip_wakeword_prefix(text, "hey_jarvis") == expected


@pytest.mark.parametrize(
    "text",
    [
        "Remind me to call Jarvis about the invoice.",
        "Bună dimineața! Am găsit 3 mesaje noi de la bancă.",
        "Turn the lights off.",
        "",
    ],
)
def test_only_a_leading_wake_phrase_is_touched(text: str):
    assert strip_wakeword_prefix(text, "hey_jarvis") == text


def test_a_bare_wake_phrase_is_kept():
    """"Hey Jarvis" on its own is a real turn — the answer is "yes?". Returning an empty
    string would look like a failed transcription and get dropped."""
    assert strip_wakeword_prefix("Hey Jarvis", "hey_jarvis") == "Hey Jarvis"
    assert strip_wakeword_prefix("Jarvis.", "hey_jarvis") == "Jarvis."
