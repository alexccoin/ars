"""The voice pipeline: wakeword -> VAD/endpointing -> ASR -> reply -> TTS, with barge-in.

Everything here is one state machine driven by one frame stream, because the alternative —
several tasks each pulling from their own copy of the microphone — is how barge-in bugs are
born. There is exactly one place that decides what happens to a frame.

Barge-in is the part that is usually wrong, so it is spelled out:

* The user may interrupt while A.R.S is THINKING or SPEAKING.
* An interrupt cancels, in this order: the synthesiser (stop generating), the turn handler
  (stop the compute request), the turn task, and the sink (drop queued audio). Muting the
  speaker while the machine keeps working is not an interrupt, it is a lie.
* After an interrupt the pipeline goes straight back to LISTENING with the interrupting
  audio already in hand, because making the user say the wakeword again in order to correct
  A.R.S is hostile.
* Interrupting requires sustained speech (`barge_in.min_speech_ms`) above a raised
  threshold, so A.R.S does not interrupt itself on the echo of its own output.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from ars_core import AsrEngine, TtsEngine, WakewordEngine
from ars_protocol import (
    AgentState,
    AudioFrame,
    AudioOut,
    Device,
    ErrorEvent,
    Language,
    ReplyDelta,
    ReplyDone,
    ServerEvent,
    Session,
    StateChanged,
    SynthesisChunk,
    Transcript,
    TranscriptEvent,
    Turn,
    VoiceActivity,
    WakeDetected,
    WakeEvent,
)

from .audio.frames import PreRollBuffer, frame_duration_ms, reseq
from .audio.sinks import AudioSink, NullSink
from .channel import FrameChannel
from .config import VoicePipelineConfig
from .handler import EchoTurnHandler, TurnHandler
from .metrics import EngineCounters, LatencyRecorder, TurnLatency
from .tts.streaming import StreamingSynthesizer
from .vad.base import FrameVadEngine
from .vad.endpointing import Endpointer, EndpointReason, endpointer_from_config

log = logging.getLogger(__name__)


class VoicePipeline:
    """One session's worth of voice."""

    def __init__(
        self,
        *,
        wakeword: WakewordEngine,
        vad: FrameVadEngine,
        asr: AsrEngine,
        tts: TtsEngine,
        handler: TurnHandler | None = None,
        config: VoicePipelineConfig | None = None,
        session: Session | None = None,
        sink: AudioSink | None = None,
        recorder: LatencyRecorder | None = None,
        counters: EngineCounters | None = None,
    ) -> None:
        self.config = config or VoicePipelineConfig()
        self.wakeword = wakeword
        self.vad = vad
        self.asr = asr
        self.tts = tts
        self.handler = handler or EchoTurnHandler()
        self.session = session or Session(device=Device.HEADLESS)
        self.sink = sink or NullSink()
        self.latency = recorder or LatencyRecorder()
        self.counters = counters or EngineCounters()
        self.endpointer: Endpointer = endpointer_from_config(self.config)
        self.synthesizer = StreamingSynthesizer(
            tts, first_sentence_max_chars=self.config.tts.first_sentence_max_chars
        )

        if self.session.preferred_language is not None:
            arbiter = getattr(self.asr, "arbiter", None)
            if arbiter is not None:
                arbiter.pinned = self.session.preferred_language

        self.state = AgentState.IDLE
        self.turn: Turn | None = None
        self.turn_latency: TurnLatency | None = None
        self.transcripts: list[Transcript] = []

        self._events: asyncio.Queue[ServerEvent | None] = asyncio.Queue()
        self._recent = PreRollBuffer(
            self.config.wakeword.pre_roll_ms + self.config.barge_in.min_speech_ms + 500
        )
        self._wake_channel: FrameChannel | None = None
        self._wake_task: asyncio.Task | None = None
        self._pending_wake: WakeEvent | None = None
        self._wake_push_mark: float = 0.0

        self._asr_channel: FrameChannel | None = None
        self._asr_task: asyncio.Task | None = None
        self._final: asyncio.Future[Transcript] | None = None

        self._turn_task: asyncio.Task | None = None
        self._barge_in_speech_ms = 0.0
        self._interrupt_requested = False
        self._expected_seq: int | None = None
        self._spoken_text = ""
        self._asr_seq = 0

    # ------------------------------------------------------------------ public

    async def run(self, frames: AsyncIterator[AudioFrame]) -> AsyncIterator[ServerEvent]:
        """Drive the pipeline over an audio stream, yielding protocol events.

        The caller (the gateway) forwards these to the client verbatim; nothing in here
        invents a message type that is not in `ars_protocol.events`.
        """
        pump = asyncio.create_task(self._pump(frames), name="voice-pump")
        try:
            while True:
                event = await self._events.get()
                if event is None:
                    break
                yield event
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
            await self._teardown()

    async def interrupt(self, reason: str = "user_cancel") -> None:
        """Client-initiated interrupt (`ars_protocol.Interrupt`). Same path as barge-in."""
        self._interrupt_requested = True
        await self._cancel_turn(reason)

    def latency_report(self, *, voice_only: bool = False) -> str:
        return self.latency.format_report(voice_only=voice_only)

    # ------------------------------------------------------------------ main loop

    async def _pump(self, frames: AsyncIterator[AudioFrame]) -> None:
        await self._start_wakeword()
        try:
            async for frame in frames:
                await self._on_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("voice pipeline failed")
            await self._emit(
                ErrorEvent(
                    code="voice.pipeline_error",
                    message=str(exc),
                    turn_id=self.turn.id if self.turn else None,
                    recoverable=False,
                )
            )
        finally:
            # Let an in-flight turn finish speaking when the audio source ends (a file
            # replay), but never wait forever for it.
            if self._turn_task is not None and not self._turn_task.done():
                with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(self._turn_task), timeout=10.0)
            await self._events.put(None)

    async def _on_frame(self, frame: AudioFrame) -> None:
        self.counters.frames_in += 1
        if self._expected_seq is not None and frame.seq != self._expected_seq:
            self.counters.frame_gaps += 1
        self._expected_seq = frame.seq + 1
        self._recent.push(frame)

        if self.state in (AgentState.IDLE, AgentState.ERROR):
            await self._feed_wakeword(frame)
        elif self.state is AgentState.LISTENING:
            await self._listen(frame)
        elif self.state in (
            AgentState.TRANSCRIBING,
            AgentState.THINKING,
            AgentState.ACTING,
            AgentState.SPEAKING,
            AgentState.WAITING_FOR_CONSENT,
        ):
            # TRANSCRIBING belongs here, not with LISTENING: the utterance is closed and a
            # turn is in flight, so speech now is an interruption, not a continuation.
            await self._watch_for_barge_in(frame)

    # ------------------------------------------------------------------ wakeword

    async def _start_wakeword(self) -> None:
        self._wake_channel = FrameChannel()
        self._wake_task = asyncio.create_task(self._consume_wakeword(), name="voice-wakeword")

    async def _consume_wakeword(self) -> None:
        assert self._wake_channel is not None
        try:
            async for event in self.wakeword.detect(self._wake_channel.frames()):
                self._pending_wake = event
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("wakeword engine failed")

    async def _feed_wakeword(self, frame: AudioFrame) -> None:
        assert self._wake_channel is not None
        self._wake_push_mark = LatencyRecorder.mark()
        await self._wake_channel.push(frame)
        # Give the detector task a chance to consume this frame before we decide whether it
        # fired. Without this hop the wake event is observed one frame (20 ms) late, which
        # would be 13% of the wakeword budget spent on scheduling.
        await asyncio.sleep(0)
        if self._pending_wake is not None:
            event, self._pending_wake = self._pending_wake, None
            await self._begin_turn(event)

    # ------------------------------------------------------------------ listening

    async def _begin_turn(self, event: WakeEvent) -> None:
        self.counters.wake_events += 1
        self.turn = Turn(session_id=self.session.id)
        self.turn_latency = self.latency.start_turn(self.turn.id)
        self.turn_latency.wake_audio_ms = self._wake_push_mark
        self.turn_latency.wake_detected_ms = LatencyRecorder.mark()
        await self._emit(WakeDetected(event=event))

        pre_roll = self._pre_roll_for(event)
        await self._enter(AgentState.LISTENING)
        await self._open_asr(pre_roll)

    def _pre_roll_for(self, event: WakeEvent) -> tuple[AudioFrame, ...]:
        """The audio from before the wakeword fired.

        Taken from the engine when it keeps its own ring (every engine deriving from
        `BufferedWakewordEngine` does), falling back to the pipeline's own recent-audio ring
        so a third-party engine still gets the behaviour.
        """
        getter = getattr(self.wakeword, "pre_roll_frames", None)
        if callable(getter):
            frames = getter(event)
            if frames:
                return frames
        return self._recent.snapshot(event.pre_roll_ms)

    async def _open_asr(self, pre_roll: tuple[AudioFrame, ...]) -> None:
        self.endpointer.reset()
        self.vad.reset()
        await self.vad.prepare()
        self._asr_channel = FrameChannel()
        self._final = asyncio.get_running_loop().create_future()
        self._asr_task = asyncio.create_task(self._consume_asr(), name="voice-asr")

        # Pre-roll first, renumbered so the spliced stream stays monotonic: a seq jump is how
        # every downstream component detects dropped audio.
        for frame in reseq(pre_roll, start=0):
            self._asr_channel.push_nowait(frame)
        self._asr_seq = len(pre_roll)
        # The pre-roll is audio the user already spoke; run it through the endpointer too, or
        # a wakeword followed immediately by speech looks like silence and endpoints at once.
        for frame in pre_roll:
            step = self.vad.step(frame)
            self.endpointer.update(step.raw_is_speech, frame_duration_ms(frame))

    async def _consume_asr(self) -> None:
        assert self._asr_channel is not None and self.turn is not None
        language = self.session.preferred_language
        try:
            async for transcript in self.asr.transcribe(
                self._asr_channel.frames(), language=language
            ):
                if transcript.is_final:
                    if self.turn_latency is not None:
                        self.turn_latency.asr_final_ms = LatencyRecorder.mark()
                    if self._final is not None and not self._final.done():
                        self._final.set_result(transcript)
                    return
                if self.turn_latency is not None and self.turn_latency.asr_first_partial_ms is None:
                    self.turn_latency.asr_first_partial_ms = LatencyRecorder.mark()
                self.transcripts.append(transcript)
                self.endpointer.note_partial(transcript.text, transcript.language)
                await self._emit(TranscriptEvent(turn_id=self.turn.id, transcript=transcript))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("ASR failed")
            if self._final is not None and not self._final.done():
                self._final.set_exception(exc)

    async def _listen(self, frame: AudioFrame) -> None:
        assert self._asr_channel is not None
        step = self.vad.step(frame)
        if step.event is not None:
            await self._emit(VoiceActivity(event=step.event))

        self._asr_channel.push_nowait(
            AudioFrame(seq=self._asr_seq, captured_at_ms=frame.captured_at_ms, pcm=frame.pcm)
        )
        self._asr_seq += 1

        # Raw, not smoothed: see VadStep.raw_is_speech. Stacking the VAD hangover on the
        # endpoint silence window costs ~200 ms on every single turn.
        decision = self.endpointer.update(step.raw_is_speech, frame_duration_ms(frame))
        if decision.speech_just_ended and self.turn_latency is not None:
            # The endpointing budget is measured from here: the frame where the user stopped.
            self.turn_latency.speech_end_ms = LatencyRecorder.mark()
        if decision.hesitation_extended and decision.speech_just_ended:
            self.counters.hesitation_extensions += 1

        if not decision.is_endpoint:
            return

        if decision.reason is EndpointReason.NO_SPEECH:
            log.info("no speech after wake; returning to idle")
            await self._abandon_turn()
            return

        if decision.reason is EndpointReason.MAX_DURATION:
            self.counters.endpoint_by_max_duration += 1
        else:
            self.counters.endpoint_by_silence += 1
        if self.turn_latency is not None:
            self.turn_latency.endpoint_ms = LatencyRecorder.mark()
        await self._close_utterance()

    async def _close_utterance(self) -> None:
        assert self._asr_channel is not None and self._final is not None
        await self._enter(AgentState.TRANSCRIBING)
        self._asr_channel.close()
        try:
            transcript = await asyncio.wait_for(self._final, timeout=15.0)
        except Exception as exc:  # includes TimeoutError
            log.warning("no final transcript: %s", exc)
            await self._emit(
                ErrorEvent(
                    code="voice.asr_failed",
                    message=str(exc) or "no final transcript",
                    turn_id=self.turn.id if self.turn else None,
                )
            )
            await self._abandon_turn()
            return

        self.counters.utterances += 1
        self.transcripts.append(transcript)
        assert self.turn is not None
        await self._emit(TranscriptEvent(turn_id=self.turn.id, transcript=transcript))
        self._turn_task = asyncio.create_task(self._run_turn(transcript), name="voice-turn")

    # ------------------------------------------------------------------ reply

    async def _run_turn(self, transcript: Transcript) -> None:
        assert self.turn is not None
        turn = self.turn
        language = self.handler.reply_language(transcript)
        spoken = ""
        try:
            await self._enter(AgentState.THINKING)
            deltas = self._instrumented_deltas(
                self.handler.respond(transcript, turn), turn, language
            )
            first_audio = True
            async for chunk in self.synthesizer.stream(
                deltas, language=language, voice=self.config.voice_for(language)
            ):
                if chunk.is_final:
                    continue
                if first_audio:
                    first_audio = False
                    if self.turn_latency is not None:
                        self.turn_latency.tts_first_audio_ms = LatencyRecorder.mark()
                    await self._enter(AgentState.SPEAKING)
                await self.sink.write(chunk)
                await self._emit(AudioOut(turn_id=turn.id, chunk=chunk))
                spoken = self._spoken_text
            await self.sink.write(SynthesisChunk(seq=0, pcm=b"", is_final=True))
            await self._emit(
                ReplyDone(turn_id=turn.id, text=self._spoken_text or spoken, language=language)
            )
        except asyncio.CancelledError:
            log.info("turn %s cancelled", turn.id)
            raise
        finally:
            # A cancelled turn is committed by `_cancel_turn`, after it has timestamped how
            # long the cancellation actually took to bite. Committing here as well would
            # both double-count the turn and lose that measurement.
            if self.turn_latency is not None and not self.turn_latency.cancelled:
                self.latency.commit(self.turn_latency)
            if self.state not in (AgentState.IDLE, AgentState.LISTENING):
                await self._enter(AgentState.IDLE)
            self._reset_turn_state()

    async def _instrumented_deltas(
        self, deltas: AsyncIterator[str], turn: Turn, language: Language
    ) -> AsyncIterator[str]:
        """Timestamp first token, emit ReplyDelta, and start the TTS clock at the moment the
        first sentence is complete — not when the reply began."""
        self._spoken_text = ""
        first = True
        async for delta in deltas:
            if first:
                first = False
                if self.turn_latency is not None:
                    self.turn_latency.reply_first_token_ms = LatencyRecorder.mark()
                    self.turn_latency.tts_requested_ms = LatencyRecorder.mark()
            self._spoken_text += delta
            await self._emit(ReplyDelta(turn_id=turn.id, text=delta, language=language))
            yield delta

    # ------------------------------------------------------------------ barge-in

    async def _watch_for_barge_in(self, frame: AudioFrame) -> None:
        if not self.config.barge_in.enabled:
            return
        step = self.vad.step(frame)
        duration = frame_duration_ms(frame)

        if step.is_speech and self._above_barge_in_threshold(step.energy_db):
            self._barge_in_speech_ms += duration
        else:
            self._barge_in_speech_ms = max(0.0, self._barge_in_speech_ms - duration)

        if self._barge_in_speech_ms >= self.config.barge_in.min_speech_ms:
            await self._cancel_turn("barge_in")

    def _above_barge_in_threshold(self, energy_db: float) -> bool:
        """While the speaker is active, demand extra margin over the VAD threshold.

        Our own output leaks into the microphone. Without the margin A.R.S interrupts itself
        halfway through every reply, which looks exactly like a barge-in bug in the state
        machine and is not.
        """
        threshold = getattr(self.vad, "effective_threshold_db", None)
        if threshold is None:
            return True
        if self.state is not AgentState.SPEAKING:
            return True
        return energy_db > threshold + self.config.barge_in.energy_margin_db

    async def _cancel_turn(self, reason: str) -> None:
        if self._turn_task is None or self._turn_task.done():
            self._barge_in_speech_ms = 0.0
            return
        self.counters.barge_ins += 1
        self.counters.tts_cancellations += 1
        if self.turn_latency is not None:
            self.turn_latency.cancel_requested_ms = LatencyRecorder.mark()
            self.turn_latency.cancelled = True

        # Order matters. Stop making audio, stop the compute request, then tear down the
        # task, then drop whatever is already queued for the speaker.
        await self.synthesizer.cancel()
        await self.handler.cancel()
        task, self._turn_task = self._turn_task, None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await self.sink.flush()

        if self.turn_latency is not None:
            self.turn_latency.cancel_effective_ms = LatencyRecorder.mark()
            self.latency.commit(self.turn_latency)
        log.info("turn cancelled: %s", reason)

        self._barge_in_speech_ms = 0.0
        self._reset_turn_state()

        if self.config.barge_in.resume_listening and reason == "barge_in":
            # The user is already mid-sentence. Keep the audio that triggered the interrupt
            # as pre-roll and go straight back to listening.
            pre_roll = self._recent.snapshot(
                self.config.barge_in.min_speech_ms + self.config.wakeword.pre_roll_ms
            )
            self.turn = Turn(session_id=self.session.id)
            self.turn_latency = self.latency.start_turn(self.turn.id)
            self.turn_latency.wake_audio_ms = LatencyRecorder.mark()
            self.turn_latency.wake_detected_ms = self.turn_latency.wake_audio_ms
            await self._enter(AgentState.LISTENING)
            await self._open_asr(pre_roll)
        else:
            await self._enter(AgentState.IDLE)

    # ------------------------------------------------------------------ housekeeping

    async def _abandon_turn(self) -> None:
        await self._close_asr()
        self.turn = None
        self.turn_latency = None
        await self._enter(AgentState.IDLE)

    def _reset_turn_state(self) -> None:
        self._final = None
        if self._asr_task is not None and not self._asr_task.done():
            self._asr_task.cancel()
        self._asr_task = None
        self._asr_channel = None

    async def _close_asr(self) -> None:
        if self._asr_channel is not None:
            self._asr_channel.close()
        if self._asr_task is not None:
            self._asr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._asr_task
        self._asr_task = None
        self._asr_channel = None
        self._final = None

    async def _enter(self, state: AgentState) -> None:
        if state is self.state:
            return
        self.state = state
        if state is AgentState.IDLE:
            self.vad.reset()
            self._barge_in_speech_ms = 0.0
        await self._emit(
            StateChanged(state=state, turn_id=self.turn.id if self.turn else None)
        )

    async def _emit(self, event: ServerEvent) -> None:
        await self._events.put(event)

    async def _teardown(self) -> None:
        await self._close_asr()
        if self._turn_task is not None:
            self._turn_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._turn_task
            self._turn_task = None
        if self._wake_channel is not None:
            self._wake_channel.close()
        if self._wake_task is not None:
            self._wake_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._wake_task
            self._wake_task = None
        await self.sink.close()
