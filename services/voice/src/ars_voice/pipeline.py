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
from .vad.endpointing import (
    Endpointer,
    EndpointReason,
    EndpointState,
    endpointer_from_config,
)
from .wakeword.prefix import strip_wakeword_prefix

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
        self._wake_seq = 0
        """Sequence numbers for the wakeword channel, which is fed only while IDLE."""

        self._asr_channel: FrameChannel | None = None
        self._asr_task: asyncio.Task | None = None
        self._final: asyncio.Future[Transcript] | None = None

        self._turn_task: asyncio.Task | None = None
        self._barge_in_speech_ms = 0.0
        self._interrupt_requested = False
        self._expected_seq: int | None = None
        self._speech_active = True
        self._turn_began_with_wake = False
        self._resuming = False
        """A cancellation is in flight that will reopen the microphone, not idle."""
        self._epoch = 0
        """Bumped every time the utterance in progress is superseded or abandoned.

        `_close_utterance` awaits a decode for up to 15 seconds, and the world can move on
        underneath it. Comparing the epoch it started with is how it finds out."""
        self._spoken_text = ""
        self._asr_seq = 0
        self._warmed = False
        self.warm_up_ms: dict[str, float] = {}

    # ------------------------------------------------------------------ public

    async def warm_up(self) -> dict[str, float]:
        """Load every model before the first turn, and report what each cost.

        Idempotent. Called automatically by `run()`; call it earlier if the client can show
        a "starting up" state, because it takes seconds and it consumes no audio while it
        runs.

        This is not tidiness. Measured on this machine: a Piper voice costs ~355 ms to load
        and ~25 ms to prime, against a 120 ms time-to-first-audio budget — and it is per
        voice, so a bilingual household blows the budget once in English and again in
        Romanian. mlx-whisper's first decode after a cold load is ~500 ms against ~130 ms
        warm. Every one of those milliseconds lands inside a real user's first turn unless
        it is paid here.
        """
        if self._warmed:
            return self.warm_up_ms
        timings: dict[str, float] = {}

        for name in ("wakeword", "vad", "asr", "tts"):
            engine = getattr(self, name)
            start = LatencyRecorder.mark()
            try:
                if name == "vad":
                    await self._prepare_vad()
                else:
                    await self._warm_engine(engine)
            except Exception as exc:
                # A warm-up failure is reported, not fatal: the engine may still work on
                # first use, and refusing to start the pipeline over it would be worse.
                log.warning("%s warm-up failed: %s", name, exc)
                await self._emit(
                    ErrorEvent(code="voice.warmup_failed", message=f"{name}: {exc}")
                )
            timings[name] = LatencyRecorder.mark() - start

        self._warmed = True
        self.warm_up_ms = timings
        log.info(
            "voice warm: %s", ", ".join(f"{k}={v:.0f} ms" for k, v in timings.items())
        )
        return timings

    async def _warm_engine(self, engine: object) -> None:
        """Best effort, duck-typed. `warm_up`/`load`/`prepare` are voice-internal extensions;
        the `packages/core` interfaces deliberately do not require them, so an engine that
        has none is simply not warmed."""
        warm = getattr(engine, "warm_up", None)
        if callable(warm):
            result = warm(self.config.languages) if engine is self.tts else warm()
            await result if asyncio.iscoroutine(result) else None
            return
        for attribute in ("load", "prepare", "_load"):
            hook = getattr(engine, attribute, None)
            if callable(hook):
                result = hook()
                if asyncio.iscoroutine(result):
                    await result
                return

    async def run(self, frames: AsyncIterator[AudioFrame]) -> AsyncIterator[ServerEvent]:
        """Drive the pipeline over an audio stream, yielding protocol events.

        The caller (the gateway) forwards these to the client verbatim; nothing in here
        invents a message type that is not in `ars_protocol.events`.
        """
        if self.config.warm_up_on_start:
            await self.warm_up()
        self._reset_stream_state()
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

    def _reset_stream_state(self) -> None:
        """Start a stream from a known state.

        A `VoicePipeline` outlives one audio stream — a client reconnects, a benchmark
        replays a fixture — and reloading the models per stream is not an option at 1.6 GB.
        So the per-stream state is cleared here instead. Leaving `self.state` at whatever the
        last stream ended on is how a failed turn poisons every turn after it.
        """
        self.state = AgentState.IDLE
        self.turn = None
        self.turn_latency = None
        self.transcripts.clear()
        self._recent.clear()
        self._pending_wake = None
        self._expected_seq = None
        self._barge_in_speech_ms = 0.0
        self._speech_active = True
        self._turn_began_with_wake = False
        self._resuming = False
        self._asr_seq = 0
        self._wake_seq = 0
        self._spoken_text = ""
        self.endpointer.reset()
        self.vad.reset()

    async def interrupt(self, reason: str = "user_cancel") -> None:
        """Client-initiated interrupt (`ars_protocol.Interrupt`). Same path as barge-in."""
        self._interrupt_requested = True
        await self._cancel_turn(reason, resume=False)

    BUSY_STATES = (
        AgentState.TRANSCRIBING,
        AgentState.THINKING,
        AgentState.ACTING,
        AgentState.SPEAKING,
        AgentState.WAITING_FOR_CONSENT,
    )

    async def request_turn(self, reason: str = "user_press") -> str:
        """"Listen to me, now." The one entry point for a deliberate push-to-talk press.

        Arming the wakeword engine is not enough on its own, and that gap cost a whole
        evening of debugging. The engine is only fed frames while the pipeline is IDLE, so
        an arm set in any other state sits there — not lost, *deferred* — until the current
        turn ends, and then opens the microphone at a moment nobody asked for. Measured on
        mock engines: three presses at 1.6 s, 3.1 s and 4.6 s during LISTENING and THINKING
        produced zero wake events and zero state changes, and the arm finally fired at
        5.06 s, 1.95 s after the last press and long after the user had stopped talking.
        Through the gateway, with a 14B model on the other end of the turn, the deferral was
        13 seconds. From the outside that is a dead button.

        So the press is routed by state instead:

        * IDLE/ERROR — arm the engine, which fires on the next inference window (<=80 ms)
          and carries the pre-roll with it. Unchanged, and still the only way a turn begins.
        * LISTENING — the microphone is already open and the words are already reaching ASR.
          Do nothing, and in particular do *not* arm: that is precisely how the stale arm
          was created.
        * anything else — the user's own hand is on the button while A.R.S is thinking or
          talking, which is not an echo and not ambiguous. Cancel the turn through the full
          cancellation path (synthesiser, compute request, task, speaker queue) and go
          straight back to listening with the audio already in hand.

        Returns what actually happened, so the caller can log something true rather than
        "armed" regardless.
        """
        if self.state in (AgentState.IDLE, AgentState.ERROR):
            trigger = getattr(self.wakeword, "trigger", None)
            if not callable(trigger):
                log.error("press: %s cannot be triggered", type(self.wakeword).__name__)
                return "unsupported"
            trigger()
            return "armed"
        if self.state is AgentState.LISTENING:
            return "already_listening"
        await self._cancel_turn(reason, resume=True)
        return "interrupted"

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
            # This is the one place that sees every frame in every state, so it is the only
            # place that can tell dropped audio from a stream we deliberately paused. Say
            # how much was lost, in milliseconds, because "frame gap" on its own sends
            # people looking for a scheduling bug when the microphone simply overran.
            lost = frame.seq - self._expected_seq
            self.counters.frame_gaps += 1
            self.counters.extra["frames_dropped"] = (
                self.counters.extra.get("frames_dropped", 0) + max(lost, 0)
            )
            log.warning(
                "microphone gap: %d frames (%.0f ms) never arrived (expected seq=%d, got %d)",
                lost, lost * frame_duration_ms(frame), self._expected_seq, frame.seq,
            )
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
        # Renumbered, exactly as the ASR channel is. The wakeword is fed only while the
        # pipeline is IDLE, so every turn leaves a hole in the raw sequence — and the
        # engine's gap detector, which cannot know the pause was deliberate, reported it as
        # dropped audio. A real 4.4 s turn produced "frame gap, expected seq=8 got 226":
        # 218 frames that were never lost, they went to ASR. Crying wolf here is not
        # harmless, it is what hides a genuine drop, which `_on_frame` above now reports.
        await self._wake_channel.push(
            AudioFrame(seq=self._wake_seq, captured_at_ms=frame.captured_at_ms, pcm=frame.pcm)
        )
        self._wake_seq += 1
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
        self._turn_began_with_wake = True
        await self._enter(AgentState.LISTENING)
        await self._open_asr(pre_roll)

    async def _prepare_vad(self) -> None:
        """Load the VAD, and fall back to the energy VAD if it will not load.

        Losing voice entirely because an ONNX runtime would not start is a much worse outcome
        than endpointing on energy for this session. The fallback is a complete engine, not a
        stub, which is the whole reason it exists — and the user is told, because silently
        degrading the thing that decides when they have finished speaking is not honest.
        """
        try:
            await self.vad.prepare()
        except Exception as exc:
            from .vad.energy import EnergyVadEngine

            log.warning("VAD failed to load (%s); falling back to the energy VAD", exc)
            await self._emit(
                ErrorEvent(
                    code="voice.vad_degraded",
                    message=f"{type(self.vad).__name__} unavailable ({exc}); using energy VAD",
                )
            )
            self.vad = EnergyVadEngine(
                threshold_db=self.config.vad.energy_threshold_db,
                adaptive=self.config.vad.energy_adaptive,
                noise_margin_db=self.config.vad.energy_noise_margin_db,
                noise_floor_halflife_ms=self.config.vad.energy_noise_floor_halflife_ms,
            )
            await self.vad.prepare()

    def _notify_speech_active(self, active: bool) -> None:
        if active == self._speech_active:
            return
        self._speech_active = active
        notify = getattr(self.asr, "set_speech_active", None)
        if callable(notify):
            notify(active)

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
        await self._prepare_vad()
        self._asr_channel = FrameChannel()
        self._speech_active = True
        self._notify_speech_active(True)
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
        if self._asr_channel is None:
            # Only reachable if opening the utterance failed part-way. An assert here would
            # kill the pump and take the session with it; going back to idle costs the user
            # one repeat of the wakeword.
            log.warning("listening with no ASR channel; returning to idle")
            await self._abandon_turn()
            return
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
        if decision.changed:
            # Tell ASR whether the user is still talking. During trailing silence a partial
            # decode is 100 ms of GPU spent on a screen update nobody will read, landing
            # directly on top of the final decode that the ASR budget is measured on.
            self._notify_speech_active(decision.state is EndpointState.SPEECH)
        if decision.speech_just_ended and self.turn_latency is not None:
            # The endpointing budget is measured from the moment the user stopped, which is
            # the *start* of this frame — the endpointer has already counted its whole
            # duration into the silence window by the time we get here. Marking "now" instead
            # under-reports by one frame (20 ms) and quietly makes the row look better than
            # the user's experience.
            self.turn_latency.speech_end_ms = (
                LatencyRecorder.mark() - frame_duration_ms(frame)
            )
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
        epoch = self._epoch
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

        if epoch != self._epoch:
            # A press arrived while the decode was in flight and has already opened a new
            # utterance. TRANSCRIBING is a short state — ~150 ms — but it is not zero, and
            # answering the abandoned question here would start a second turn on top of the
            # live one: two `_turn_task`s, two replies, one of which nobody asked for.
            log.info("dropping a transcript the user has already moved past")
            return

        transcript = self._strip_wakeword(transcript)
        self.counters.utterances += 1
        self.transcripts.append(transcript)
        assert self.turn is not None
        await self._emit(TranscriptEvent(turn_id=self.turn.id, transcript=transcript))
        self._turn_task = asyncio.create_task(self._run_turn(transcript), name="voice-turn")

    # ------------------------------------------------------------------ reply

    def _strip_wakeword(self, transcript: Transcript) -> Transcript:
        """Remove the keyword the pre-roll dragged in. Only for wake-initiated turns.

        A turn that began as a barge-in has no wake phrase in front of it, and stripping
        there would eat a real first word.
        """
        if not self.config.wakeword.strip_from_transcript or not self._turn_began_with_wake:
            return transcript
        cleaned = strip_wakeword_prefix(transcript.text, self.config.core.wakeword)
        if cleaned == transcript.text:
            return transcript
        log.debug("stripped wake phrase: %r -> %r", transcript.text, cleaned)
        self.counters.extra["wakeword_prefix_stripped"] = (
            self.counters.extra.get("wakeword_prefix_stripped", 0) + 1
        )
        return transcript.model_copy(update={"text": cleaned})

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
            # `_resuming` is set by a cancellation that is about to reopen the microphone.
            # Without it the client sees THINKING -> IDLE -> LISTENING for an interrupt that
            # never idled: the button in the interface blinks off and back on, and the
            # gateway's re-arm-on-idle fires a press into a pipeline that is already
            # listening. An interrupt-and-listen is one transition, so it emits one.
            if not self._resuming and self.state not in (AgentState.IDLE, AgentState.LISTENING):
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

    async def _cancel_turn(self, reason: str, *, resume: bool | None = None) -> None:
        """Cancel the turn in flight.

        `resume` says whether to go back to LISTENING with the recent audio as pre-roll,
        rather than to IDLE. It is a parameter and not a lookup of
        `barge_in.resume_listening` because the two callers mean different things: a
        barge-in resumes only if barge-in says so, an explicit press always resumes (the
        user pressed *in order to speak*), and the stop button never resumes. Left as None
        it keeps the barge-in rule, which is what `_watch_for_barge_in` wants.
        """
        if resume is None:
            resume = self.config.barge_in.resume_listening and reason == "barge_in"
        if self._turn_task is None or self._turn_task.done():
            self._barge_in_speech_ms = 0.0
            if resume:
                # Nothing to cancel, but the user still asked to speak. Open the microphone
                # rather than returning silently, which is the whole defect being fixed.
                await self._resume_listening()
            return
        self.counters.barge_ins += 1
        self.counters.tts_cancellations += 1
        if self.turn_latency is not None:
            self.turn_latency.cancel_requested_ms = LatencyRecorder.mark()
            self.turn_latency.cancelled = True

        # Order matters. Stop making audio, stop the compute request, then tear down the
        # task, then drop whatever is already queued for the speaker.
        self._resuming = resume
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
        self._resuming = False

        if resume:
            await self._resume_listening()
        else:
            await self._enter(AgentState.IDLE)

    def _supersede(self) -> None:
        """Whatever utterance was in flight is no longer the one being answered."""
        self._epoch += 1

    async def _resume_listening(self) -> None:
        """Open a fresh utterance immediately, with the audio already in hand as pre-roll.

        Used after a barge-in and after an explicit press. In both cases the user is
        already talking, or about to; making them wait for a wakeword to be re-armed would
        clip the first word off the thing they interrupted for."""
        self._supersede()
        pre_roll = self._recent.snapshot(
            self.config.barge_in.min_speech_ms + self.config.wakeword.pre_roll_ms
        )
        self.turn = Turn(session_id=self.session.id)
        self.turn_latency = self.latency.start_turn(self.turn.id)
        self.turn_latency.wake_audio_ms = LatencyRecorder.mark()
        self.turn_latency.wake_detected_ms = self.turn_latency.wake_audio_ms
        # No wake phrase in front of this audio, so nothing to strip: stripping here would
        # eat a real first word.
        self._turn_began_with_wake = False
        await self._enter(AgentState.LISTENING)
        await self._open_asr(pre_roll)

    # ------------------------------------------------------------------ housekeeping

    async def _abandon_turn(self) -> None:
        self._supersede()
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
