"""faster-whisper backend — large-v3-turbo, int8.

The only place faster-whisper may be imported, and it is imported inside `_load()`.

Whisper is not a streaming model: it decodes a fixed window. "Streaming partials" here means
re-decoding a growing buffer on a timer with a cheap beam, and decoding once properly at the
end. The partial decodes run in a worker thread and are dropped if a newer one is due, so a
slow partial can never delay the final — the final is the one on the latency budget.

UNVERIFIED: no weights are installed in this checkout, so this class has never executed
against a real model. Everything below is written against the faster-whisper 1.1 API and is
defensive about version differences, but the numbers in the latency report come from the
mock path only. See services/voice/README.md.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np
from ars_core import AsrEngine
from ars_protocol import (
    SAMPLE_RATE_HZ,
    SUPPORTED_LANGUAGES,
    AudioFrame,
    Language,
    Transcript,
    TranscriptSegment,
)

from ..audio.frames import bytes_for_ms, frame_duration_ms, pcm_to_float32
from .language import LanguageArbiter, constrain_probabilities

log = logging.getLogger(__name__)


class FasterWhisperEngine(AsrEngine):
    """Bilingual EN/RO streaming ASR."""

    name = "faster-whisper"

    def __init__(
        self,
        *,
        model: str = "large-v3-turbo",
        compute_type: str = "int8",
        device: str = "auto",
        model_dir: Path | str = "./models/asr",
        partial_interval_ms: float = 480.0,
        partial_window_ms: float = 8_000.0,
        beam_size_partial: int = 1,
        beam_size_final: int = 5,
        vad_filter: bool = True,
        arbiter: LanguageArbiter | None = None,
        cpu_threads: int = 0,
    ) -> None:
        self.model_name = model
        self.compute_type = compute_type
        self.device = device
        self.model_dir = Path(model_dir)
        self.partial_interval_ms = partial_interval_ms
        self.partial_window_ms = partial_window_ms
        self.beam_size_partial = beam_size_partial
        self.beam_size_final = beam_size_final
        self.vad_filter = vad_filter
        self.arbiter = arbiter or LanguageArbiter()
        self.cpu_threads = cpu_threads
        self._model = None
        self._load_lock = asyncio.Lock()

    @property
    def supported_languages(self) -> tuple[Language, ...]:
        return SUPPORTED_LANGUAGES

    # ------------------------------------------------------------------ model

    def _model_target(self) -> str:
        """A local directory if the fetch script has run, otherwise the hub id.

        Preferring the local copy matters for more than speed: the reference deployment has
        no network, and a model that silently downloads on first use turns "local-first" into
        "local once it has phoned home".
        """
        local = self.model_dir / self.model_name
        return str(local) if local.is_dir() else self.model_name

    async def load(self) -> None:
        """Preload the weights. Call at startup: the first load is seconds, and paying it
        inside the first turn blows the budget for that turn by an order of magnitude."""
        await self._load()

    async def _load(self) -> None:
        if self._model is not None:
            return
        async with self._load_lock:
            if self._model is not None:
                return
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise RuntimeError(
                    "FasterWhisperEngine needs the 'asr' extra: "
                    "uv pip install -e 'services/voice[asr]', then scripts/fetch_voice_models.sh"
                ) from exc
            target = self._model_target()
            log.info(
                "loading faster-whisper %s device=%s compute_type=%s",
                target, self.device, self.compute_type,
            )
            self._model = await asyncio.to_thread(
                WhisperModel,
                target,
                device=self.device,
                compute_type=self.compute_type,
                cpu_threads=self.cpu_threads,
                download_root=str(self.model_dir),
            )

    # ------------------------------------------------------------------ interface

    async def transcribe(
        self,
        frames: AsyncIterator[AudioFrame],
        *,
        language: Language | None = None,
    ) -> AsyncIterator[Transcript]:
        await self._load()
        buffer = bytearray()
        audio_ms = 0.0
        next_partial_at = self.partial_interval_ms
        partial_task: asyncio.Task | None = None
        last_partial_text = ""

        try:
            async for frame in frames:
                buffer += frame.pcm
                audio_ms += frame_duration_ms(frame)

                if partial_task is not None and partial_task.done():
                    text = partial_task.result()
                    partial_task = None
                    if text and text != last_partial_text:
                        last_partial_text = text
                        yield Transcript(
                            text=text,
                            language=language or self.arbiter.current,
                            language_confidence=0.0,
                            # Partials carry no language claim worth acting on: whisper's
                            # detection on 500 ms of audio is close to a coin flip, and the
                            # UI must not flicker between languages while the user speaks.
                            is_final=False,
                            audio_duration_ms=int(audio_ms),
                        )

                if audio_ms >= next_partial_at and partial_task is None:
                    next_partial_at = audio_ms + self.partial_interval_ms
                    window = bytes(buffer[-bytes_for_ms(self.partial_window_ms) :])
                    partial_task = asyncio.create_task(
                        asyncio.to_thread(self._decode_partial, window, language)
                    )
        finally:
            if partial_task is not None:
                partial_task.cancel()

        final = await asyncio.to_thread(self._decode_final, bytes(buffer), language, audio_ms)
        yield final

    # ------------------------------------------------------------------ decoding

    def _audio(self, pcm: bytes) -> np.ndarray:
        return pcm_to_float32(pcm)

    def _decode_partial(self, pcm: bytes, language: Language | None) -> str:
        if self._model is None or not pcm:
            return ""
        segments, _info = self._model.transcribe(
            self._audio(pcm),
            language=(language or self.arbiter.current).value,
            beam_size=self.beam_size_partial,
            best_of=1,
            temperature=0.0,
            condition_on_previous_text=False,
            without_timestamps=True,
            vad_filter=False,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()

    def _detect_language(self, audio: np.ndarray) -> tuple[Language, float]:
        """Detect, constrained to the languages A.R.S supports.

        Two code paths because faster-whisper moved this API: `detect_language` exists in
        >=1.0.2, older builds only expose the distribution through `transcribe(language=None)`.
        Both end at `constrain_probabilities`, so the constraint cannot be bypassed.
        """
        model = self._model
        assert model is not None
        detect = getattr(model, "detect_language", None)
        if callable(detect):
            try:
                _lang, _prob, all_probs = detect(audio)
                if all_probs:
                    return constrain_probabilities(dict(all_probs), self.supported_languages)
            except Exception:  # pragma: no cover - version drift
                log.debug("detect_language unavailable, falling back", exc_info=True)
        _segments, info = model.transcribe(
            audio, language=None, beam_size=1, without_timestamps=True
        )
        probs = dict(getattr(info, "all_language_probs", None) or {})
        if probs:
            return constrain_probabilities(probs, self.supported_languages)
        detected = getattr(info, "language", Language.EN.value)
        confidence = float(getattr(info, "language_probability", 0.0))
        for lang in self.supported_languages:
            if lang.value == detected:
                return lang, confidence
        return self.arbiter.current, 0.0

    def _decode_final(
        self, pcm: bytes, language: Language | None, audio_ms: float
    ) -> Transcript:
        model = self._model
        assert model is not None
        audio = self._audio(pcm)

        if language is not None:
            resolved, confidence = language, 1.0
        else:
            detected, detected_confidence = self._detect_language(audio)
            # The text is not known yet, so the word count that gates a weak switch is not
            # available. Decode in the detected language first, then let the arbiter judge
            # with the transcript in hand; if it refuses the switch, re-decode. The re-decode
            # only happens on a short, ambiguous, language-changing utterance — rare, and the
            # alternative is flipping the whole conversation because the user said "ok".
            resolved, confidence = detected, detected_confidence

        segments, _info = model.transcribe(
            audio,
            language=resolved.value,
            beam_size=self.beam_size_final,
            temperature=0.0,
            condition_on_previous_text=False,
            vad_filter=self.vad_filter,
            word_timestamps=False,
        )
        collected = list(segments)
        text = " ".join(s.text.strip() for s in collected).strip()

        if language is None:
            decision = self.arbiter.decide(resolved, confidence, text=text)
            if decision.language is not resolved:
                log.info("language arbiter: %s", decision.reason)
                segments, _info = model.transcribe(
                    audio,
                    language=decision.language.value,
                    beam_size=self.beam_size_final,
                    temperature=0.0,
                    condition_on_previous_text=False,
                    vad_filter=self.vad_filter,
                )
                collected = list(segments)
                text = " ".join(s.text.strip() for s in collected).strip()
            resolved, confidence = decision.language, decision.confidence

        return Transcript(
            text=text,
            language=resolved,
            language_confidence=min(max(confidence, 0.0), 1.0),
            is_final=True,
            segments=tuple(
                TranscriptSegment(
                    text=s.text.strip(),
                    start_ms=max(0, int(s.start * 1000)),
                    end_ms=max(0, int(s.end * 1000)),
                    confidence=_segment_confidence(s),
                )
                for s in collected
            ),
            audio_duration_ms=int(audio_ms if audio_ms else audio.size / SAMPLE_RATE_HZ * 1000),
        )


def _segment_confidence(segment) -> float | None:
    logprob = getattr(segment, "avg_logprob", None)
    if logprob is None:
        return None
    # avg_logprob is roughly [-1, 0] for good decodes. Squash into [0, 1] for the protocol's
    # Confidence field without pretending it is a calibrated probability.
    return float(min(1.0, max(0.0, 1.0 + logprob)))
