"""The mic button must do something, in every state, and say which thing it did.

A spoken turn worked perfectly in a single process and produced nothing at all through the
desktop app. The press was armed — the log said so — and the wakeword fired, and no
transcript ever arrived. Both facts were true and neither meant what it looked like.

The wakeword engine is fed frames only while the pipeline is IDLE. An arm set in any other
state is therefore not lost, it is *deferred*: it sits there until the current turn ends and
then opens the microphone at a moment nobody asked for. Measured on the mock engines below,
before the fix: presses at 1.60 s, 3.11 s and 4.61 s, during LISTENING and THINKING,
produced zero wake events and zero state changes; the arm finally fired at 5.06 s, 1.95 s
after the last press and long after the speaker had stopped talking. Through the gateway,
with a 14B model on the other end of the turn, the same deferral was measured at 13 seconds.
From the outside that is a dead button — and the firing the user eventually sees in the log
belongs to a press they made several sentences ago.

These tests are about that: a press reaches the state machine, and the state machine answers
honestly.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from ars_protocol import AgentState, Device, Session
from ars_voice.asr.mock import MockAsrEngine
from ars_voice.audio.frames import frames_from_pcm
from ars_voice.audio.sinks import NullSink
from ars_voice.config import VoicePipelineConfig
from ars_voice.handler import TurnHandler
from ars_voice.pipeline import VoicePipeline
from ars_voice.tts.mock import MockTtsEngine
from ars_voice.vad.energy import EnergyVadEngine
from ars_voice.wakeword.manual import ManualWakewordEngine
from voice_helpers import silence, speech


class SlowHandler(TurnHandler):
    """Stands in for the tier ladder. A real reply is seconds, not microseconds — which is
    exactly the window in which the user presses again, and exactly what the single-process
    test with an echo handler could never reproduce."""

    def __init__(self, seconds: float = 2.5) -> None:
        self.seconds = seconds
        self.cancelled = False

    async def respond(self, transcript, turn):
        await asyncio.sleep(self.seconds)
        yield "twenty five working days"

    async def cancel(self) -> None:
        self.cancelled = True


def build(*, script: list[str], handler: TurnHandler | None = None) -> VoicePipeline:
    config = VoicePipelineConfig()
    config.tts.backend = "mock"
    return VoicePipeline(
        wakeword=ManualWakewordEngine(pre_roll_ms=config.wakeword.pre_roll_ms),
        vad=EnergyVadEngine(),
        asr=MockAsrEngine(script),
        tts=MockTtsEngine(chunk_ms=config.tts.chunk_ms, realtime_factor=60.0),
        handler=handler or SlowHandler(),
        config=config,
        session=Session(device=Device.HEADLESS),
        sink=NullSink(),
    )


def one_question() -> list:
    return frames_from_pcm(
        silence(300) + speech(1200, seed=4) + silence(1100, seed=6) + silence(9000, seed=9)
    )


async def _until_thinking(pipeline: VoicePipeline) -> None:
    """Wait for the turn to actually reach the model. An `asyncio.Event` would be tidier
    but the pipeline does not expose one, and polling a state machine is honest here."""
    while pipeline.state is not AgentState.THINKING:  # noqa: ASYNC110
        await asyncio.sleep(0.01)


async def realtime(frames) -> object:
    for frame in frames:
        await asyncio.sleep(0.02)
        yield frame


# --------------------------------------------------------------------------- routing

@pytest.mark.asyncio
async def test_a_press_while_thinking_opens_the_microphone_now_not_later() -> None:
    """The bug, in one assertion.

    The user asked something, A.R.S is working on it, and the user presses again to ask
    something else. Arming the wakeword achieves nothing here — no frames reach it while a
    turn is in flight — so the press has to be routed as what it plainly is: this person
    wants to talk, and their hand is on the button, so it is not an echo and not ambiguous.
    Cancel the turn through the full path and listen.
    """
    pipeline = build(script=["what is the annual paid leave", "and how much notice"])
    states: list[AgentState] = []
    outcome: list[str] = []

    async def press_once_thinking() -> None:
        await pipeline.request_turn()  # from IDLE: arms, as before
        await _until_thinking(pipeline)
        outcome.append(await pipeline.request_turn())

    presser = asyncio.create_task(press_once_thinking())

    async def drive() -> None:
        async for event in pipeline.run(realtime(one_question())):
            if getattr(event, "type", None) == "state":
                states.append(event.state)
                if outcome and event.state is AgentState.LISTENING:
                    return

    await asyncio.wait_for(drive(), timeout=15)
    presser.cancel()

    assert outcome == ["interrupted"]
    assert states[-1] is AgentState.LISTENING, (
        "the press was swallowed: the pipeline is not listening to the person pressing"
    )


@pytest.mark.asyncio
async def test_pressing_while_thinking_stops_the_model_that_is_thinking() -> None:
    """Cancellation has to reach the compute request, not just the state machine. Leaving
    a 14B model generating tokens for an answer nobody will hear is the user's battery, and
    on a cloud backend their money."""
    handler = SlowHandler(seconds=5.0)
    pipeline = build(script=["what is the annual paid leave", "and how much notice"],
                     handler=handler)

    async def press_once_thinking() -> None:
        await pipeline.request_turn()
        await _until_thinking(pipeline)
        await pipeline.request_turn()

    presser = asyncio.create_task(press_once_thinking())

    async def drive() -> None:
        async for _ in pipeline.run(realtime(one_question())):
            if handler.cancelled:
                return

    await asyncio.wait_for(drive(), timeout=15)
    presser.cancel()
    assert handler.cancelled, "the reply was abandoned but the model was left running"


@pytest.mark.asyncio
async def test_a_press_while_already_listening_does_not_leave_an_arm_behind() -> None:
    """The microphone is already open and the words are already reaching ASR, so there is
    nothing to do. Arming anyway is precisely how the stale arm was created: it fires on the
    *next* return to idle and starts a turn out of nowhere, several seconds later, with the
    room as its input."""
    pipeline = build(script=["what is the annual paid leave"])
    pipeline.state = AgentState.LISTENING

    assert await pipeline.request_turn() == "already_listening"
    assert not pipeline.wakeword._armed, "a press left an arm that will fire on its own later"


@pytest.mark.asyncio
async def test_a_press_from_idle_still_goes_through_the_wakeword() -> None:
    """The fix must not add a second way to start a turn. One press, one `WakeEvent`, one
    pre-roll — two entry points into a turn is how one of them ends up not cancelling
    properly."""
    pipeline = build(script=["what is the annual paid leave"])

    assert await pipeline.request_turn() == "armed"
    assert pipeline.wakeword._armed


@pytest.mark.asyncio
async def test_an_interrupt_and_listen_is_one_transition_not_a_blink() -> None:
    """THINKING -> LISTENING, with no IDLE in between.

    The interrupt never idled, and saying it did has consequences beyond a flickering
    button: the gateway re-arms the wakeword on every return to idle, so a phantom IDLE
    fires a phantom press into a pipeline that is already listening."""
    pipeline = build(script=["what is the annual paid leave", "and how much notice"])
    states: list[AgentState] = []

    async def press_once_thinking() -> None:
        await pipeline.request_turn()
        await _until_thinking(pipeline)
        await pipeline.request_turn()

    presser = asyncio.create_task(press_once_thinking())

    async def drive() -> None:
        async for event in pipeline.run(realtime(one_question())):
            if getattr(event, "type", None) == "state":
                states.append(event.state)
                if len(states) >= 2 and states[-1] is AgentState.LISTENING and (
                    AgentState.THINKING in states
                ):
                    return

    await asyncio.wait_for(drive(), timeout=15)
    presser.cancel()

    after_thinking = states[states.index(AgentState.THINKING) + 1:]
    assert after_thinking and after_thinking[0] is AgentState.LISTENING, (
        f"expected THINKING -> LISTENING, got THINKING -> {[s.value for s in after_thinking]}"
    )


@pytest.mark.asyncio
async def test_the_stop_button_still_stops_rather_than_reopening_the_microphone() -> None:
    """A press and an interrupt are different intentions and must stay different. The stop
    button means "be quiet", not "listen to me" — reopening the microphone on it would put
    A.R.S back to listening on open speakers it has just been talking through, which is the
    self-conversation this project already paid for once."""
    pipeline = build(script=["what is the annual paid leave"])
    states: list[AgentState] = []

    async def interrupt_when_thinking() -> None:
        await pipeline.request_turn()
        await _until_thinking(pipeline)
        await pipeline.interrupt("user_cancel")

    presser = asyncio.create_task(interrupt_when_thinking())

    async def drive() -> None:
        async for event in pipeline.run(realtime(one_question())):
            if getattr(event, "type", None) == "state":
                states.append(event.state)
                if AgentState.THINKING in states and event.state is AgentState.IDLE:
                    return

    await asyncio.wait_for(drive(), timeout=15)
    presser.cancel()

    after_thinking = states[states.index(AgentState.THINKING) + 1:]
    assert after_thinking[0] is AgentState.IDLE
    assert AgentState.LISTENING not in after_thinking


# --------------------------------------------------------------------------- honest gaps

@pytest.mark.asyncio
async def test_a_turn_is_not_reported_as_dropped_audio() -> None:
    """"wakeword: frame gap, expected seq=4 got 1077" — 21 seconds of audio, apparently
    thrown away. It was not: the wakeword is fed only while IDLE, so every turn leaves a
    hole in the raw sequence and the engine, which cannot know the pause was deliberate,
    reported it as loss. That warning cost an evening of hunting a starved event loop.

    Crying wolf in an audio path is not harmless. It is what hides a real drop.
    """
    pipeline = build(script=["what is the annual paid leave"])
    warnings: list[str] = []

    import logging

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.WARNING:
                warnings.append(record.getMessage())

    handler = Collect()
    logging.getLogger("ars_voice").addHandler(handler)
    try:
        async def drive() -> None:
            async for event in pipeline.run(realtime(one_question())):
                if getattr(event, "type", None) == "state" and (
                    event.state is AgentState.THINKING
                ):
                    return

        await pipeline.request_turn()
        await asyncio.wait_for(drive(), timeout=15)
    finally:
        logging.getLogger("ars_voice").removeHandler(handler)

    assert not [w for w in warnings if "gap" in w], warnings
    assert pipeline.counters.frame_gaps == 0


@pytest.mark.asyncio
async def test_audio_that_really_went_missing_is_reported_in_milliseconds() -> None:
    """The other half of the same coin. The pipeline is the only place that sees every
    frame in every state, so it is the only place that can tell a deliberate pause from
    loss — and when it is loss it says how much, because "frame gap" on its own sends
    people looking for a scheduling bug when the microphone simply overran."""
    pipeline = build(script=["what is the annual paid leave"])
    await pipeline._start_wakeword()
    frames = frames_from_pcm(silence(400))
    holed = frames[:5] + frames[15:]  # ten frames, 200 ms, gone

    try:
        for frame in holed:
            await pipeline._on_frame(frame)
    finally:
        await pipeline._teardown()

    assert pipeline.counters.frame_gaps == 1
    assert pipeline.counters.extra["frames_dropped"] == 10


@pytest.mark.asyncio
async def test_a_press_during_the_decode_does_not_answer_the_abandoned_question() -> None:
    """TRANSCRIBING is short — roughly 150 ms on real engines — but it is not zero.

    `_close_utterance` awaits the final decode, and a press that lands in that window opens
    a new utterance underneath it. Two things then went wrong at once, and each is worse
    than the bug being fixed:

    * The decode came back and started a reply for the question the user had already moved
      past — a second turn in flight on top of the live one, a second voice.
    * The ASR task resolved `self._final`, which by then belonged to the *new* utterance.
      So the new question inherited the old question's text, and the old `_close_utterance`
      sat out its full 15-second timeout on a future nobody would ever complete.

    The decode is slowed to 600 ms here so the window is real rather than lucky.
    """
    config = VoicePipelineConfig()
    config.tts.backend = "mock"
    pipeline = VoicePipeline(
        wakeword=ManualWakewordEngine(pre_roll_ms=config.wakeword.pre_roll_ms),
        vad=EnergyVadEngine(),
        asr=MockAsrEngine(
            ["what is the annual paid leave", "and how much notice"], final_decode_ms=600.0
        ),
        tts=MockTtsEngine(chunk_ms=config.tts.chunk_ms, realtime_factor=60.0),
        handler=SlowHandler(),
        config=config,
        session=Session(device=Device.HEADLESS),
        sink=NullSink(),
    )
    saw_transcribing = asyncio.Event()
    finals: list[str] = []

    await pipeline.request_turn()

    async def press_during_the_decode() -> None:
        await saw_transcribing.wait()
        await asyncio.sleep(0.1)  # squarely inside the 600 ms decode
        await pipeline.request_turn()

    presser = asyncio.create_task(press_during_the_decode())

    async def drive() -> None:
        async for event in pipeline.run(realtime(one_question())):
            kind = getattr(event, "type", None)
            if kind == "state" and event.state is AgentState.TRANSCRIBING:
                saw_transcribing.set()
            if kind == "transcript" and event.transcript.is_final:
                finals.append(event.transcript.text)

    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(drive(), timeout=6)
    presser.cancel()

    assert saw_transcribing.is_set(), "the test never reached the window it is about"
    assert finals == [], f"a superseded utterance was answered anyway: {finals}"
    assert pipeline.counters.utterances == 0, (
        "the abandoned question was counted, and therefore replied to"
    )
