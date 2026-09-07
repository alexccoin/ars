"""One session, several questions, one press.

The microphone button is a switch, not a trigger: while it is on, every return to idle
re-arms, so a user who paused to think does not find the microphone silently deaf. That
behaviour had never been exercised anywhere — the loop hardcoded a real `MicrophoneSource`,
so testing it required a laptop, a microphone and a person willing to talk into it, which
is the same as saying it was not tested. A second question in the same session either works
or the conversation is one question long, and nobody had checked which.

These tests drive `VoiceLoop` with a scripted pipeline and no audio hardware at all.
"""

from __future__ import annotations

import asyncio

import pytest
from ars_gateway.voice import VoiceLoop
from ars_protocol import AgentState, Device, Session, StateChanged


class ScriptedPipeline:
    """A pipeline that emits the state changes a real turn emits, and records presses.

    Deliberately not a `VoicePipeline`: the question here is what the *loop* does with the
    events it gets back, and a real pipeline would answer that question with models."""

    def __init__(self, beats: list[object]) -> None:
        self._beats = beats
        self.state = AgentState.IDLE
        self.requests: list[str] = []
        self.frames_seen = 0
        self.warm_up_ms: dict[str, float] = {}
        self.counters = type("C", (), {"frame_gaps": 0})()
        self.latency = type("L", (), {"turns": ()})()

    async def warm_up(self) -> dict[str, float]:
        return {}

    async def request_turn(self, reason: str = "user_press") -> str:
        self.requests.append(reason)
        return "armed"

    async def interrupt(self, reason: str = "user_cancel") -> None:
        self.requests.append(f"interrupt:{reason}")

    async def run(self, frames):
        async def drain() -> None:
            async for _ in frames:
                self.frames_seen += 1

        task = asyncio.create_task(drain())
        try:
            for beat in self._beats:
                await asyncio.sleep(0.01)
                yield beat
        finally:
            task.cancel()


async def _silence_source():
    """A microphone that works: silence is audio, and it never stops arriving."""
    from ars_protocol import AudioFrame, BYTES_PER_FRAME

    seq = 0
    while True:
        await asyncio.sleep(0.001)
        yield AudioFrame(seq=seq, pcm=b"\x00" * BYTES_PER_FRAME)
        seq += 1


def build(beats: list[object]) -> tuple[VoiceLoop, ScriptedPipeline, list[dict]]:
    events: list[dict] = []
    loop = VoiceLoop(
        handler=None,  # never reached: the pipeline is scripted
        session=Session(device=Device.HEADLESS),
        on_event=events.append,
        source=lambda: type("S", (), {"frames": staticmethod(_silence_source)})(),
    )
    pipeline = ScriptedPipeline(beats)
    loop._pipeline = pipeline
    return loop, pipeline, events


@pytest.mark.asyncio
async def test_the_loop_re_arms_after_a_turn_so_a_second_question_works() -> None:
    """The switch, doing its job. A turn ends, the pipeline goes back to idle, and because
    the user has not switched the microphone off, the loop presses again on their behalf.
    Without this the answer to "what is the annual paid leave?" is the last thing A.R.S
    ever hears from that session."""
    loop, pipeline, _ = build([
        StateChanged(state=AgentState.LISTENING),
        StateChanged(state=AgentState.THINKING),
        StateChanged(state=AgentState.IDLE),
        StateChanged(state=AgentState.LISTENING),
        StateChanged(state=AgentState.IDLE),
    ])
    loop._holding = True

    await loop.start()
    await asyncio.wait_for(loop._task, timeout=5)
    await asyncio.sleep(0.05)  # let the presses the loop scheduled actually run

    assert pipeline.requests.count("user_press") == 2, (
        f"the microphone was left switched on but only re-armed {pipeline.requests} times"
    )


@pytest.mark.asyncio
async def test_switching_the_microphone_off_stops_the_re_arming() -> None:
    """`release()` is the other half of the switch. If a return to idle re-armed regardless,
    turning the microphone off would leave it open — the difference between a product that
    listens when asked and one that listens always."""
    loop, pipeline, _ = build([
        StateChanged(state=AgentState.IDLE),
        StateChanged(state=AgentState.IDLE),
    ])
    loop._holding = True
    loop.release()

    await loop.start()
    await asyncio.wait_for(loop._task, timeout=5)
    await asyncio.sleep(0.05)

    assert pipeline.requests == []


@pytest.mark.asyncio
async def test_a_press_goes_through_the_pipeline_not_straight_at_the_engine() -> None:
    """`press()` used to call `trigger()` on the wakeword engine and log "press: armed",
    which was true and useless — arming does nothing unless the pipeline is IDLE, because
    that is the only state in which the engine is fed frames. Routing it through
    `request_turn` is what makes a press mean something in every state."""
    loop, pipeline, _ = build([StateChanged(state=AgentState.LISTENING)])

    await loop.start()
    loop.press()
    await asyncio.wait_for(loop._task, timeout=5)
    await asyncio.sleep(0.05)

    assert "user_press" in pipeline.requests
    assert loop._holding is True, "a real press turns the microphone switch on"


@pytest.mark.asyncio
async def test_the_loop_never_claims_silence_when_no_audio_arrived() -> None:
    """A working capture produces a frame every 20 ms, silence included. "No audio" and
    "quiet room" are different signals, and on macOS a microphone that was never granted
    permission looks exactly like the second one unless this is said out loud."""
    events: list[dict] = []

    async def deaf():
        await asyncio.sleep(0.05)
        return
        yield  # pragma: no cover - never reached, makes this an async generator

    loop = VoiceLoop(
        handler=None,
        session=Session(device=Device.HEADLESS),
        on_event=events.append,
        source=lambda: type("S", (), {"frames": staticmethod(deaf)})(),
    )
    loop._pipeline = ScriptedPipeline([])

    await loop.start()
    await asyncio.wait_for(loop._task, timeout=5)

    assert any(e.get("code") == "NoAudio" for e in events)
