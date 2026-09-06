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
    SAMPLE_RATE_HZ, Language, ServerEvent, Session, Transcript, Turn,
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
                 on_event: Callable[[dict], object], language: Language | None = None) -> None:
        self._handler = handler
        self._session = session
        self._on_event = on_event
        self._language = language
        self._pipeline = None
        self._wake: ManualWakewordEngine | None = None
        self._sink: SpeakerSink | None = None
        self._task: asyncio.Task | None = None
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
            self.warm_up_ms = await self._pipeline.warm_up()
            log.info("voice warm: %s", self.warm_up_ms)
            self._task = asyncio.create_task(self._pump(), name="voice-loop")

    async def _pump(self) -> None:
        mic = MicrophoneSource()
        try:
            async for event in self._pipeline.run(mic.frames()):
                await self._emit(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("voice loop failed")
            await self._emit_dict({
                "type": "error", "code": type(exc).__name__,
                "message": f"the microphone stopped: {exc}", "recoverable": True,
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

    def press(self) -> None:
        """The mic button. Starts one turn, with the half second before the press."""
        if self._wake is not None:
            self._wake.trigger()

    async def interrupt(self) -> None:
        if self._pipeline is not None:
            await self._pipeline.interrupt("user_cancel")

    async def stop(self) -> None:
        async with self._lock:
            if self._task is not None:
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
                self._task = None
            if self._sink is not None:
                with contextlib.suppress(Exception):
                    await self._sink.close()
