"""The orchestrator: a full turn in both languages, pre-roll delivery, and barge-in.

Barge-in is the reason this file is long. The requirement is not "playback stops"; it is
that cancellation reaches the synthesiser *and* the in-flight compute request, and that the
user can keep talking without saying the wakeword again.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from ars_protocol import (
    AgentState,
    AudioFrame,
    Device,
    Language,
    Session,
    Transcript,
    Turn,
)
from ars_voice.asr.mock import MockAsrEngine, ScriptedUtterance
from ars_voice.audio.frames import frames_from_pcm, pcm_from_frames
from ars_voice.audio.sinks import NullSink
from ars_voice.audio.sources import frames_from_iterable
from ars_voice.config import VoicePipelineConfig
from ars_voice.eval.wakeword import marker_score
from ars_voice.handler import EchoTurnHandler, TurnHandler
from ars_voice.metrics import Stage
from ars_voice.pipeline import VoicePipeline
from ars_voice.tts.mock import MockTtsEngine
from ars_voice.vad.energy import EnergyVadEngine
from ars_voice.wakeword.mock import MockWakewordEngine
from voice_helpers import keyword, silence, speech


def build(
    *, script, handler=None, tts=None, config=None, sink=None, asr=None
) -> VoicePipeline:
    config = config or VoicePipelineConfig()
    return VoicePipeline(
        wakeword=MockWakewordEngine(
            score_fn=marker_score,
            threshold=config.core.wakeword_threshold,
            pre_roll_ms=config.wakeword.pre_roll_ms,
        ),
        vad=EnergyVadEngine(),
        asr=asr or MockAsrEngine(script),
        tts=tts or MockTtsEngine(chunk_ms=config.tts.chunk_ms, realtime_factor=60.0),
        handler=handler or EchoTurnHandler(),
        config=config,
        session=Session(device=Device.HEADLESS),
        sink=sink or NullSink(),
    )


async def feed(frames: list[AudioFrame], delay: float = 0.0) -> AsyncIterator[AudioFrame]:
    for frame in frames:
        await asyncio.sleep(delay)
        yield frame


def turn_audio(*, utterance_ms: float = 1_200, trailing_ms: float = 1_100) -> list[AudioFrame]:
    """Keyword, then an utterance with no gap, then enough silence to endpoint."""
    return frames_from_pcm(
        silence(300) + keyword(480) + speech(utterance_ms, seed=4) + silence(trailing_ms, seed=6)
    )


class RecordingAsr(MockAsrEngine):
    """A mock ASR that keeps every frame it was handed, so the test can prove the pre-roll
    audio actually reached it rather than being merely retained somewhere."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.received: list[AudioFrame] = []

    async def transcribe(self, frames, *, language=None):
        async def capture():
            async for frame in frames:
                self.received.append(frame)
                yield frame

        async for transcript in super().transcribe(capture(), language=language):
            yield transcript


# --------------------------------------------------------------------------- happy path

@pytest.mark.parametrize(
    ("language", "text", "expected_prefix"),
    [
        (Language.EN, "turn the lights off in the kitchen", "You said"),
        (Language.RO, "stinge lumina din bucătărie", "Ai spus"),
    ],
)
async def test_a_full_turn_round_trips_in_both_languages(language, text, expected_prefix):
    pipeline = build(script=[ScriptedUtterance(text, language, 0.94)])
    events = [e async for e in pipeline.run(feed(turn_audio()))]
    kinds = [e.type for e in events]

    assert "wake" in kinds
    assert "transcript" in kinds
    assert "audio_out" in kinds
    assert "reply_done" in kinds

    final = [e.transcript for e in events if e.type == "transcript" and e.transcript.is_final]
    assert len(final) == 1
    assert final[0].text == text
    assert final[0].language is language

    reply = next(e for e in events if e.type == "reply_done")
    assert reply.language is language
    assert reply.text.startswith(expected_prefix)

    states = [e.state for e in events if e.type == "state"]
    assert AgentState.LISTENING in states
    assert AgentState.SPEAKING in states
    assert states[-1] is AgentState.IDLE
    assert pipeline.counters.utterances == 1
    assert pipeline.counters.endpoint_by_silence == 1


async def test_the_pre_roll_audio_reaches_asr_so_the_first_syllable_is_not_lost():
    """The ring buffer is only useful if its contents actually reach ASR.

    The lead-in is long enough that the buffer is full at detection, so the test also proves
    the pre-roll is bounded — it hands ASR the configured window of audio, not the whole
    session, which would be both a latency problem and a privacy one.
    """
    asr = RecordingAsr([ScriptedUtterance("turn the lights off", Language.EN, 0.94)])
    pipeline = build(script=[], asr=asr)
    lead_in_ms, keyword_ms = 1_500, 480
    frames = frames_from_pcm(
        silence(lead_in_ms) + keyword(keyword_ms) + speech(1_000, seed=4) + silence(1_100, seed=6)
    )
    async for _ in pipeline.run(feed(frames)):
        pass

    assert asr.received, "ASR received nothing"
    assert [f.seq for f in asr.received] == list(range(len(asr.received))), (
        "spliced pre-roll broke seq monotonicity — downstream reads that as dropped audio"
    )

    pre_roll_ms = pipeline.config.wakeword.pre_roll_ms
    received = pcm_from_frames(asr.received)
    original = pcm_from_frames(frames)
    assert received in original, "ASR was handed audio that was never captured"
    offset_ms = original.index(received) / 32

    command_starts_at = lead_in_ms + keyword_ms
    assert offset_ms < command_starts_at, "ASR audio starts after the keyword — first syllable lost"
    assert command_starts_at - offset_ms >= 300, "less than 300 ms of lead-in survived"
    assert offset_ms > 0, "the pre-roll is not bounded — it handed over the whole session"
    assert offset_ms >= lead_in_ms - pre_roll_ms


async def test_a_wakeword_with_no_command_produces_an_empty_final_and_returns_to_idle():
    """'hey A.R.S' and then nothing. It must not hang with the microphone open, and it must
    still produce exactly one final transcript — a state machine waiting on `is_final` would
    otherwise deadlock the session."""
    pipeline = build(script=[])
    frames = frames_from_pcm(silence(300) + keyword(480) + silence(1_600))
    events = [e async for e in pipeline.run(feed(frames))]

    finals = [e.transcript for e in events if e.type == "transcript" and e.transcript.is_final]
    assert len(finals) == 1
    assert finals[0].text == ""
    reply = next(e for e in events if e.type == "reply_done")
    assert reply.text == EchoTurnHandler.EMPTY[Language.EN]
    assert [e.state for e in events if e.type == "state"][-1] is AgentState.IDLE


async def test_latency_recorder_populates_the_voice_owned_budget_rows():
    """Audio is paced at wall clock here on purpose.

    The endpointing row is a duration the *user* experiences. Replaying frames as fast as
    the loop will take them measures nothing and reports ~1 ms, which is how a latency
    regression hides in a green test suite.
    """
    pipeline = build(script=[ScriptedUtterance("turn the lights off", Language.EN, 0.94)])
    frames = frames_from_pcm(
        silence(300) + keyword(480) + speech(600, seed=4) + silence(1_000, seed=6)
    )
    async for _ in pipeline.run(frames_from_iterable(frames, realtime=True)):
        pass

    for stage in (Stage.WAKEWORD_DETECTION, Stage.ENDPOINTING, Stage.ASR_FINAL,
                  Stage.TTS_FIRST_AUDIO):
        assert pipeline.latency.samples(stage), f"no sample recorded for {stage.value}"

    endpointing = pipeline.latency.stats(Stage.ENDPOINTING)
    assert endpointing.p95_ms == pytest.approx(
        pipeline.config.core.endpoint_silence_ms, abs=120
    ), "endpointing latency does not match the configured silence window"
    assert pipeline.latency.stats(Stage.WAKEWORD_DETECTION).within_budget is True
    assert pipeline.latency.stats(Stage.TTS_FIRST_AUDIO).within_budget is True
    assert "endpointing" in pipeline.latency_report()


async def test_the_wake_phrase_is_stripped_from_the_final_transcript():
    """The pre-roll deliberately contains the tail of the keyword, so ASR transcribes it.
    Measured on the real pipeline: "Jarvis, good morning, I found three new messages..."."""
    config = VoicePipelineConfig()
    config.core.wakeword = "hey_jarvis"
    pipeline = build(
        script=[
            ScriptedUtterance(
                "Jarvis, turn the lights off in the kitchen", Language.EN, 0.94
            )
        ],
        config=config,
    )
    events = [e async for e in pipeline.run(feed(turn_audio()))]
    final = next(
        e.transcript for e in events if e.type == "transcript" and e.transcript.is_final
    )
    assert final.text == "Turn the lights off in the kitchen"
    assert pipeline.counters.extra["wakeword_prefix_stripped"] == 1


async def test_a_barge_in_utterance_keeps_its_first_word():
    """No wake phrase precedes a barge-in, so stripping there would eat a real first word."""
    handler = BlockingHandler()
    config = VoicePipelineConfig()
    config.core.wakeword = "hey_jarvis"
    pipeline = build(
        script=[
            ScriptedUtterance("Jarvis, turn the lights off", Language.EN, 0.94),
            ScriptedUtterance("Jarvis is not what I said", Language.EN, 0.9),
        ],
        handler=handler,
        config=config,
        tts=MockTtsEngine(chunk_ms=120, realtime_factor=3.0),
    )
    speaking = asyncio.Event()
    finals = []
    async for event in pipeline.run(barge_in_audio(speaking)):
        if event.type == "audio_out" and event.chunk.pcm and not speaking.is_set():
            speaking.set()
        if event.type == "transcript" and event.transcript.is_final:
            finals.append(event.transcript)

    assert len(finals) == 2
    assert finals[0].text == "Turn the lights off", "wake turn should be stripped"
    assert finals[1].text == "Jarvis is not what I said", "barge-in turn must not be stripped"


async def test_stripping_can_be_turned_off():
    config = VoicePipelineConfig()
    config.core.wakeword = "hey_jarvis"
    config.wakeword.strip_from_transcript = False
    pipeline = build(
        script=[ScriptedUtterance("Jarvis, turn the lights off", Language.EN, 0.94)],
        config=config,
    )
    events = [e async for e in pipeline.run(feed(turn_audio()))]
    final = next(
        e.transcript for e in events if e.type == "transcript" and e.transcript.is_final
    )
    assert final.text == "Jarvis, turn the lights off"


# --------------------------------------------------------------------------- barge-in

class BlockingHandler(TurnHandler):
    """Speaks one long sentence, then blocks until released.

    Keeps the turn deterministically in flight so the barge-in tests are about cancellation
    rather than about winning a race with the scheduler.
    """

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.cancellations = 0
        self.calls = 0
        self.finished_calls: list[int] = []
        self.closed_calls: list[int] = []

    async def respond(self, transcript: Transcript, turn: Turn) -> AsyncIterator[str]:
        self.calls += 1
        call = self.calls
        try:
            yield (
                "This is the first sentence of a deliberately long reply, long enough that "
                "synthesis is still running when the user talks over it. "
            )
            await self.release.wait()
            yield "And this is the second sentence."
            self.finished_calls.append(call)
        finally:
            # Reached when the pipeline tears the generator down. This is the observable
            # proof that cancellation propagated into the compute request itself.
            self.closed_calls.append(call)

    async def cancel(self) -> None:
        self.cancellations += 1
        self.release.set()


async def barge_in_audio(speaking: asyncio.Event) -> AsyncIterator[AudioFrame]:
    """Wake, speak, endpoint, wait until A.R.S is actually producing audio, then talk over it."""
    seq = 0
    for frame in frames_from_pcm(
        silence(300) + keyword(480) + speech(1_000, seed=4) + silence(1_000, seed=6)
    ):
        yield AudioFrame(seq=seq, pcm=frame.pcm)
        seq += 1
        await asyncio.sleep(0)
    await asyncio.wait_for(speaking.wait(), timeout=10)
    for frame in frames_from_pcm(speech(700, seed=8) + silence(1_100, seed=9)):
        yield AudioFrame(seq=seq, pcm=frame.pcm)
        seq += 1
        await asyncio.sleep(0)


async def test_barge_in_cancels_synthesis_and_the_turn():
    handler = BlockingHandler()
    # Slow generation on purpose: the cancellation that matters is the one that lands while
    # the synthesiser is still producing audio, not after it has already finished.
    tts = MockTtsEngine(chunk_ms=120, realtime_factor=3.0)
    pipeline = build(
        script=[
            ScriptedUtterance("turn the lights off", Language.EN, 0.94),
            ScriptedUtterance("no wait, the other one", Language.EN, 0.9),
        ],
        handler=handler,
        tts=tts,
    )

    speaking = asyncio.Event()
    events = []
    first_turn: str | None = None
    stale_audio = 0
    barged = False
    async for event in pipeline.run(barge_in_audio(speaking)):
        events.append(event)
        if event.type == "transcript" and first_turn is None:
            first_turn = event.turn_id
        if event.type == "audio_out" and event.chunk.pcm and not speaking.is_set():
            speaking.set()
        if not barged and pipeline.counters.barge_ins:
            barged = True
        elif barged and event.type == "audio_out" and event.turn_id == first_turn:
            # Audio for the *interrupted* turn arriving after the interruption. Audio for
            # the turn that follows is expected and is not counted.
            stale_audio += 1

    assert pipeline.counters.barge_ins == 1, "the interruption was never detected"
    # 1. synthesis was cancelled at the engine, not merely muted
    assert tts.cancellations >= 1
    assert tts.interrupted >= 1, "the synthesiser kept generating after cancel()"
    # 2. the compute request was cancelled
    assert handler.cancellations == 1
    assert 1 in handler.closed_calls, "the reply generator was never torn down"
    assert 1 not in handler.finished_calls, "the handler ran to completion on a cancelled turn"
    # 3. queued audio was dropped rather than played out
    assert pipeline.sink.flushed >= 1
    assert stale_audio == 0, "audio for the interrupted turn kept flowing after the barge-in"
    # 4. the interrupted turn never claims to have finished speaking
    assert not any(
        e.type == "reply_done" and e.turn_id == first_turn for e in events
    ), "an interrupted turn reported reply_done"


async def test_after_a_barge_in_the_user_keeps_talking_without_saying_the_wakeword_again():
    handler = BlockingHandler()
    pipeline = build(
        script=[
            ScriptedUtterance("turn the lights off", Language.EN, 0.94),
            ScriptedUtterance("nu, cealaltă lampă", Language.RO, 0.92),
        ],
        handler=handler,
        tts=MockTtsEngine(chunk_ms=120, realtime_factor=3.0),
    )

    speaking = asyncio.Event()
    events = []
    async for event in pipeline.run(barge_in_audio(speaking)):
        events.append(event)
        if event.type == "audio_out" and event.chunk.pcm and not speaking.is_set():
            speaking.set()

    wakes = [e for e in events if e.type == "wake"]
    assert len(wakes) == 1, "the user should not have to re-wake to correct A.R.S"

    states = [e.state for e in events if e.type == "state"]
    speaking_at = states.index(AgentState.SPEAKING)
    assert AgentState.LISTENING in states[speaking_at:], (
        "the pipeline did not return to listening after the interruption"
    )
    finals = [e.transcript for e in events if e.type == "transcript" and e.transcript.is_final]
    assert len(finals) == 2, "the interrupting utterance was not transcribed"
    assert finals[1].language is Language.RO
    assert finals[1].text == "nu, cealaltă lampă"


async def test_barge_in_cancellation_latency_is_measured():
    handler = BlockingHandler()
    pipeline = build(
        script=[
            ScriptedUtterance("turn the lights off", Language.EN, 0.94),
            ScriptedUtterance("stop", Language.EN, 0.5),
        ],
        handler=handler,
        tts=MockTtsEngine(chunk_ms=120, realtime_factor=3.0),
    )
    speaking = asyncio.Event()
    async for event in pipeline.run(barge_in_audio(speaking)):
        if event.type == "audio_out" and event.chunk.pcm and not speaking.is_set():
            speaking.set()

    samples = pipeline.latency.samples(Stage.BARGE_IN_CANCEL)
    assert samples, "barge-in cancellation was not timed"
    assert samples[0] < 250, f"cancellation took {samples[0]:.0f} ms to take effect"


async def test_barge_in_can_be_disabled():
    handler = BlockingHandler()
    config = VoicePipelineConfig()
    config.barge_in.enabled = False
    pipeline = build(
        script=[ScriptedUtterance("turn the lights off", Language.EN, 0.94)],
        handler=handler,
        config=config,
        tts=MockTtsEngine(chunk_ms=120, realtime_factor=3.0),
    )

    speaking = asyncio.Event()

    async def audio():
        async for frame in barge_in_audio(speaking):
            yield frame
        handler.release.set()

    async for event in pipeline.run(audio()):
        if event.type == "audio_out" and event.chunk.pcm and not speaking.is_set():
            speaking.set()

    assert pipeline.counters.barge_ins == 0
    assert handler.cancellations == 0
    assert handler.finished_calls == [1]


async def test_an_explicit_client_interrupt_cancels_the_turn():
    """`ars_protocol.Interrupt` from the client takes the same path as acoustic barge-in."""
    handler = BlockingHandler()
    tts = MockTtsEngine(chunk_ms=120, realtime_factor=3.0)
    pipeline = build(
        script=[ScriptedUtterance("turn the lights off", Language.EN, 0.94)],
        handler=handler,
        tts=tts,
    )
    speaking = asyncio.Event()

    async def audio():
        for frame in turn_audio(trailing_ms=1_100):
            yield frame
            await asyncio.sleep(0)
        await asyncio.wait_for(speaking.wait(), timeout=10)
        await pipeline.interrupt("user_cancel")
        for frame in frames_from_pcm(silence(200)):
            yield frame
            await asyncio.sleep(0)

    events = []
    async for event in pipeline.run(audio()):
        events.append(event)
        if event.type == "audio_out" and event.chunk.pcm and not speaking.is_set():
            speaking.set()

    assert tts.cancellations >= 1
    assert handler.cancellations == 1
    assert not any(e.type == "reply_done" for e in events)
    assert [e.state for e in events if e.type == "state"][-1] is AgentState.IDLE
