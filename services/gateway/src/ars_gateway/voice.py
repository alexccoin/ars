"""Voice, wired to the same brain as the text console.

`services/voice` owns the microphone, endpointing, ASR and speech; `services/compute` owns
reasoning; neither imports the other. This is the gateway's job — the `TurnHandler` seam
that `services/voice` defines exists precisely so this file can exist and those two do not
have to know about each other.

The important property: a spoken question goes through the *same* `answer_stream` as a
typed one, so it climbs the same tier ladder. Asking "cât este chiria" out loud costs the
same 8 ms and no GPU as typing it, and the answer is spoken from the same passage. A
second path for voice would be a second set of bugs, and a second place to forget the
guard.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable

from ars_protocol import (
    FRAME_MS, SAMPLE_RATE_HZ, Language, ServerEvent, Session, Transcript, Turn,
)
from ars_voice.audio.sinks import SpeakerSink
from ars_voice.audio.sources import MicrophoneSource
from ars_voice.config import VoicePipelineConfig
from ars_voice.factory import build_pipeline
from ars_voice.handler import TurnHandler
from ars_voice.wakeword.manual import ManualWakewordEngine

log = logging.getLogger("ars.gateway.voice")

AnswerStream = Callable[..., AsyncIterator[dict]]


class LadderTurnHandler(TurnHandler):
    """Speaks whatever the tier ladder answers.

    Takes `answer_stream` as a callable rather than importing it, because `app` imports
    this module: passing the function in keeps the dependency pointing one way.
    """

    def __init__(self, answer_stream: AnswerStream, *, cancel_backend=None) -> None:
        self._answer_stream = answer_stream
        self._cancel_backend = cancel_backend
        self._cancelled = asyncio.Event()

    async def respond(self, transcript: Transcript, turn: Turn) -> AsyncIterator[str]:
        self._cancelled.clear()
        stream = self._answer_stream(transcript.text, language=transcript.language)
        try:
            async for event in stream:
                if self._cancelled.is_set():
                    break
                # Only the words are spoken. Tier verdicts, tool calls and state changes
                # are for the screen — reading "found in contract.txt, 97% match" aloud
                # every turn is how a voice assistant becomes unbearable.
                if event.get("type") == "reply_delta" and event.get("text"):
                    yield event["text"]
        finally:
            await stream.aclose()

    async def cancel(self) -> None:
        """Barge-in. Stop generating, and stop the model that is generating.

        Closing the stream alone would leave the local model producing tokens nobody will
        ever hear — the user's battery, and on a cloud backend their money.
        """
        self._cancelled.set()
        if self._cancel_backend is not None:
            with contextlib.suppress(Exception):
                await self._cancel_backend()


class VoiceLoop:
    """The microphone, the pipeline and the speakers, for one gateway.

    Built lazily: the models are ~1.6 GB and take seconds to load, and a user who only
    ever types should never pay for that. Once built it is kept — reloading per turn is
    not an option at that size.
    """

    def __init__(self, *, handler: TurnHandler, session: Session,
                 on_event: Callable[[dict], object], language: Language | None = None,
                 source: Callable[[], object] | None = None) -> None:
        self._source = source
        """Where audio comes from. Defaults to the machine's microphone.

        A seam, not a feature: without it nothing about this loop — re-arming between
        turns, what a press does while A.R.S is talking, whether a dropped frame is
        noticed — can be tested anywhere except on a laptop with a microphone and a person
        willing to talk into it. That is how all of it went untested."""
        self._handler = handler
        self._session = session
        self._on_event = on_event
        self._language = language
        self._pipeline = None
        self._wake: ManualWakewordEngine | None = None
        self._frames = 0
        """Frames seen since the loop started. Zero is a diagnosis, not a quiet room."""
        self._holding = False
        """True while the user has the microphone switched on.

        A press used to arm exactly one turn, and the pipeline gives a turn six seconds to
        hear speech before returning to idle. Miss that window — pause to think, or press
        before you are ready — and the microphone is silently deaf, with nothing on screen
        to say so. "I speak but I don't get any reactions back" is what that feels like.

        So the button is a switch, not a trigger: while it is on, every return to idle
        re-arms. Safe now that barge-in is off, because the pipeline does not listen while
        it is speaking and cannot hear itself."""
        self._sink: SpeakerSink | None = None
        self._mic: object | None = None
        self._task: asyncio.Task | None = None
        self._presses: set[asyncio.Task] = set()
        """Strong references to in-flight presses. A task nobody holds can be garbage
        collected mid-await, which would lose the press silently — the exact class of bug
        this whole path exists to stop."""
        self._lock = asyncio.Lock()
        self.warm_up_ms: dict[str, float] = {}

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _build(self) -> None:
        config = VoicePipelineConfig()
        # The desktop shell has no microphone API of its own (WKWebView does not expose
        # getUserMedia), so capture happens here, in Python, and the button in the
        # interface is a press — hence 'manual' rather than a spoken keyword.
        config.wakeword.backend = "manual"
        config.tts.backend = "piper"
        self._sink = SpeakerSink()
        self._pipeline = build_pipeline(
            config, handler=self._handler, session=self._session, sink=self._sink
        )
        self._wake = self._pipeline.wakeword

    async def start(self) -> None:
        """Load the models and begin consuming the microphone. Idempotent."""
        async with self._lock:
            if self.running:
                return
            if self._pipeline is None:
                await asyncio.to_thread(self._build)
            self._frames = 0
            self.warm_up_ms = await self._pipeline.warm_up()
            log.info("voice warm: %s", self.warm_up_ms)
            self._task = asyncio.create_task(self._pump(), name="voice-loop")

    SILENCE_ALARM_S = 4.0
    """How long a live microphone may deliver nothing before we say so.

    A working capture produces a frame every 20 ms, silence included — "no audio" and
    "quiet room" are completely different signals at this layer. When the microphone
    cannot be opened at all, sounddevice does not always raise: the stream simply never
    calls back, `frames()` never yields, the pipeline never sees a frame, and the whole
    loop sits there producing nothing. Alex pressed the button and got no reaction at all,
    twice, because failure looked exactly like a quiet room.
    """

    async def _pump(self) -> None:
        mic = self._source() if self._source is not None else MicrophoneSource()
        self._mic = mic
        watchdog = asyncio.create_task(self._warn_if_deaf(), name="voice-watchdog")
        try:
            async for event in self._pipeline.run(self._counted(mic.frames())):
                await self._emit(event)
                if getattr(event, "type", None) == "reply_done":
                    self._log_turn_latency()
                # Re-arm on every return to idle, so a missed window costs a moment
                # rather than the whole conversation.
                if (self._holding and getattr(event, "type", None) == "state"
                        and getattr(event.state, "value", None) == "idle"):
                    self.press(hold=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("voice loop failed")
            await self._emit_dict({
                "type": "error", "code": type(exc).__name__,
                "message": f"the microphone stopped: {exc}", "recoverable": True,
            })
        finally:
            watchdog.cancel()
            if self._frames == 0:
                # The loop ended without a single frame. Say so rather than stopping
                # quietly: a silent failure here is indistinguishable from working.
                await self._emit_dict({
                    "type": "error", "code": "NoAudio",
                    "message": (
                        "The microphone delivered no audio at all. On macOS this is "
                        "almost always the permission prompt not being granted to this "
                        "app — check System Settings › Privacy & Security › Microphone."
                    ),
                    "recoverable": True,
                })
            await self._emit_dict({"type": "listening", "on": False, "warming": False})

    async def _counted(self, frames):
        async for frame in frames:
            self._frames += 1
            yield frame

    def _log_turn_latency(self) -> None:
        """One line per spoken turn, with the numbers the budget is written in.

        Without it the only latency evidence for a real turn is wall-clock timestamps in a
        log full of SQL. It is also the only place the microphone's drop count is ever
        looked at, which is how a drop stayed silent long enough to be mistaken for the
        cause of a completely different bug.
        """
        if self._pipeline is None:
            return
        turns = self._pipeline.latency.turns
        if not turns:
            return
        row = " ".join(
            f"{stage.value}={ms:.0f}ms" for stage, ms in turns[-1].durations().items()
        )
        dropped = getattr(self._mic, "dropped_frames", 0)
        log.info(
            "turn latency: %s | frames=%d dropped=%d gaps=%d",
            row or "(nothing measured)", self._frames, dropped,
            self._pipeline.counters.frame_gaps,
        )
        if dropped:
            log.warning(
                "the microphone dropped %d frames (%.0f ms) this session — the pipeline is "
                "not keeping up with capture", dropped, dropped * FRAME_MS,
            )

    async def _warn_if_deaf(self) -> None:
        await asyncio.sleep(self.SILENCE_ALARM_S)
        if self._frames == 0:
            log.error("microphone delivered no frames in %.0fs", self.SILENCE_ALARM_S)
            await self._emit_dict({
                "type": "error", "code": "NoAudio",
                "message": (
                    "I cannot hear the microphone — no audio is arriving at all. This is "
                    "not a quiet room: a working microphone sends silence too."
                ),
                "recoverable": True,
            })

    async def _emit(self, event: ServerEvent) -> None:
        import json

        # `AudioOut` carries the synthesised PCM, which is not text and cannot cross a
        # JSON socket — serialising it raises `invalid utf-8 sequence` and takes the whole
        # voice loop down with it, mid-sentence. It also does not need to cross: the
        # samples are already on their way to this machine's speakers through the sink.
        # The interface only needs to know that sound is playing, so it can show A.R.S
        # speaking and let the user interrupt.
        if getattr(event, "type", None) == "audio_out":
            chunk = event.chunk
            # 16-bit mono at the protocol rate — the only format the pipeline speaks.
            duration_ms = len(chunk.pcm) / 2 / SAMPLE_RATE_HZ * 1000
            await self._emit_dict({
                "type": "audio_out", "turn_id": event.turn_id,
                "seq": chunk.seq, "duration_ms": round(duration_ms, 1),
                "is_final": chunk.is_final,
            })
            return
        await self._emit_dict(json.loads(event.model_dump_json()))

    async def _emit_dict(self, payload: dict) -> None:
        result = self._on_event(payload)
        if asyncio.iscoroutine(result):
            await result

    def press(self, *, hold: bool = False) -> None:
        """The mic button. Starts a turn, with the half second before the press.

        `hold` distinguishes the user pressing from the loop re-arming itself; only a real
        press turns listening on.

        Stays synchronous because `Ars.listen` calls it without awaiting; the work is handed
        to a task on the loop that is already running underneath both callers.
        """
        if not hold:
            self._holding = True
        if self._pipeline is None:
            log.error("press: no pipeline — the press went nowhere")
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop: nothing is consuming audio either, so arming is all that is
            # meaningful and the turn will begin when the pump starts.
            if self._wake is not None:
                self._wake.trigger()
            return
        task = loop.create_task(self._press(hold=hold), name="voice-press")
        self._presses.add(task)
        task.add_done_callback(self._presses.discard)

    async def _press(self, *, hold: bool) -> None:
        """Route the press through the pipeline and log what actually happened.

        It used to log "press: armed" unconditionally, which was true and useless: arming
        the engine does nothing at all unless the pipeline is IDLE, because that is the only
        state in which the wakeword is fed frames. Alex saw "press: armed" followed by a
        wakeword firing and concluded the press had worked — the firing belonged to the
        loop's own re-arm from the previous turn, and his press had been deferred behind a
        13-second reply. A log line that cannot distinguish those two is worse than none.
        """
        assert self._pipeline is not None
        state = getattr(self._pipeline.state, "value", "?")
        try:
            outcome = await self._pipeline.request_turn("user_press")
        except Exception:
            log.exception("press failed")
            return
        log.info("press: %s (state was %s, hold=%s)", outcome, state, hold)
        if outcome == "unsupported":
            await self._emit_dict({
                "type": "error", "code": "NoWakeword",
                "message": "the microphone button is not wired to a wakeword engine",
                "recoverable": False,
            })

    def release(self) -> None:
        """The user switched the microphone off. Stop re-arming; finish any turn in
        flight, because cutting off a question halfway through is worse than answering
        one the user has stopped caring about."""
        self._holding = False

    async def interrupt(self) -> None:
        if self._pipeline is not None:
            await self._pipeline.interrupt("user_cancel")

    async def stop(self) -> None:
        async with self._lock:
            self._holding = False
            for press in list(self._presses):
                press.cancel()
            self._presses.clear()
            if self._task is not None:
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
                self._task = None
            if self._sink is not None:
                with contextlib.suppress(Exception):
                    await self._sink.close()
