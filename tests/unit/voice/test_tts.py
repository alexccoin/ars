"""TTS: sentence-boundary streaming (time-to-first-audio) and cancellation that bites."""

from __future__ import annotations

import asyncio
import time

import pytest
from ars_protocol import Language, SynthesisRequest
from ars_voice.tts.mock import MockTtsEngine
from ars_voice.tts.segmentation import SentenceStreamer, split_sentences
from ars_voice.tts.streaming import StreamingSynthesizer


def test_sentence_split_en_and_ro():
    assert split_sentences("Turn the lights off. Anything else?") == [
        "Turn the lights off.",
        "Anything else?",
    ]
    assert split_sentences("Am stins lumina. Mai vrei ceva?") == [
        "Am stins lumina.",
        "Mai vrei ceva?",
    ]


def test_abbreviations_do_not_end_a_sentence():
    """'dl.' and 'Dr.' mid-sentence would otherwise stop Piper dead mid-clause."""
    assert split_sentences("I emailed Dr. Ionescu about it. Done.") == [
        "I emailed Dr. Ionescu about it.",
        "Done.",
    ]
    assert split_sentences("Am scris dl. Ionescu ieri. Gata.") == [
        "Am scris dl. Ionescu ieri.",
        "Gata.",
    ]
    assert split_sentences("It costs 3.50 euro. Fine.") == ["It costs 3.50 euro.", "Fine."]


def test_a_paragraph_long_first_sentence_does_not_hold_first_audio_hostage():
    text = "and then " * 40 + "finally something."
    sentences = split_sentences(text, first_max_chars=140)
    assert len(sentences[0]) <= 140
    assert len(sentences) > 1


def test_streamer_emits_the_first_sentence_before_the_rest_arrives():
    streamer = SentenceStreamer()
    assert streamer.push("Turn the lights ") == []
    assert streamer.push("off. And ") == ["Turn the lights off."]
    assert streamer.push("close the blinds.") == []
    assert streamer.flush() == ["And close the blinds."]


async def test_synthesis_streams_chunks_and_ends_with_one_final():
    engine = MockTtsEngine(chunk_ms=120)
    request = SynthesisRequest(text="Turn the lights off. Done.", language=Language.EN)
    chunks = [c async for c in engine.synthesize(request)]
    assert len(chunks) > 3
    assert chunks[-1].is_final and chunks[-1].pcm == b""
    assert [c.seq for c in chunks] == list(range(len(chunks)))
    assert all(c.pcm for c in chunks[:-1])


@pytest.mark.parametrize("language", [Language.EN, Language.RO])
async def test_both_languages_synthesise_and_pick_their_own_voice(language: Language):
    engine = MockTtsEngine(voice_en="en_US-amy-medium", voice_ro="ro_RO-mihai-medium")
    text = "Turn the lights off." if language is Language.EN else "Stinge lumina."
    chunks = [c async for c in engine.synthesize(SynthesisRequest(text=text, language=language))]
    assert sum(len(c.pcm) for c in chunks) > 0
    assert engine.voice_for(language) == (
        "ro_RO-mihai-medium" if language is Language.RO else "en_US-amy-medium"
    )


async def test_cancel_stops_generation_not_just_playback():
    """The assertion that matters: after cancel(), the engine stops *producing* audio."""
    engine = MockTtsEngine(chunk_ms=120, realtime_factor=4.0)
    long_reply = " ".join(["This is a fairly long sentence that takes a while to speak."] * 6)
    produced = 0

    async def consume():
        nonlocal produced
        async for _ in engine.synthesize(SynthesisRequest(text=long_reply, language=Language.EN)):
            produced += 1

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.08)
    at_cancel = produced
    await engine.cancel()
    await task

    assert engine.cancelled
    assert at_cancel > 0, "test did not actually reach the speaking phase"
    assert produced - at_cancel <= 1, "generation continued after cancel"
    assert not engine.completed, "a cancelled synthesis must not report completion"


async def test_streaming_synthesizer_speaks_the_first_sentence_before_the_reply_ends():
    """Time-to-first-audio is measured from the first sentence, not the last token."""
    engine = MockTtsEngine(chunk_ms=120, realtime_factor=50.0)
    synthesizer = StreamingSynthesizer(engine)
    released = asyncio.Event()

    async def deltas():
        yield "Turn the lights off. "
        await released.wait()
        yield "Anything else?"

    started = time.perf_counter()
    first_audio_at = None
    chunks = []

    async def run():
        nonlocal first_audio_at
        async for chunk in synthesizer.stream(deltas(), language=Language.EN):
            if first_audio_at is None and chunk.pcm:
                first_audio_at = time.perf_counter() - started
            chunks.append(chunk)

    task = asyncio.create_task(run())
    await asyncio.sleep(0.15)
    assert first_audio_at is not None, "no audio until the whole reply arrived"
    released.set()
    await task
    assert synthesizer.sentences_spoken == 2
    assert chunks[-1].is_final
    assert [c.seq for c in chunks] == list(range(len(chunks)))


async def test_streaming_synthesizer_cancel_reaches_the_engine():
    engine = MockTtsEngine(chunk_ms=120, realtime_factor=4.0)
    synthesizer = StreamingSynthesizer(engine)

    async def deltas():
        yield "One long sentence that keeps going and going and going. "
        yield "And a second one just as long, so there is plenty left to cancel."

    collected = []

    async def run():
        async for chunk in synthesizer.stream(deltas(), language=Language.RO):
            collected.append(chunk)

    task = asyncio.create_task(run())
    await asyncio.sleep(0.08)
    await synthesizer.cancel()
    await task

    assert engine.cancellations == 1
    assert synthesizer.cancelled
    assert not any(c.is_final for c in collected), "cancelled stream must not look complete"
