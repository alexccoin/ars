"""mlx-whisper backend — large-v3-turbo on the Apple Silicon GPU.

The only place `mlx_whisper` / `mlx` may be imported, and both are imported inside methods.

**Why this exists.** `FasterWhisperEngine` is correct and portable and, on this hardware, far
too slow: CTranslate2 has no Metal backend, so large-v3-turbo int8 decodes on the CPU while
the GPU sits idle. Measured on an M5 Max, on Piper-synthesised speech, same model family:

| backend | EN (5.9 s audio) | RO (4.8 s audio) | realtime factor |
|---|---:|---:|---|
| faster-whisper int8 CPU | 8237 ms | 8333 ms | 0.7x / 0.6x |
| **mlx-whisper GPU (this)** | **121 ms** | **135 ms** | **49x / 36x** |

Identical transcripts, both languages detected at confidence 1.00. The ASR budget row is
250 ms; one of these fits and one is off by a factor of thirty.

**Call shape, and why it is not `mlx_whisper.transcribe()`.** The utterances a voice
assistant handles fit in whisper's single 30 s window, and the encoder — which dominates the
cost — only needs to run once for that window. So:

1. one `decode(task="lang_id")` pass: runs the encoder, returns the audio features *and*
   the full language distribution;
2. constrain that distribution to EN+RO (`asr/language.py`) and pick the winner;
3. one `decode(language=...)` pass fed the features from step 1 — no second encoder pass.

`mlx_whisper.transcribe(language=None)` would do its own detection but throws the
distribution away (its `DecodingResult` list is built without `language_probs`), leaving no
confidence to report and no way to constrain to the two languages A.R.S supports. It is also
slower: 450 ms EN / 495 ms RO for the same audio, measured. `transcribe()` is still used for
the rare utterance longer than 30 s, where the chunking loop is needed.
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

from ..audio.frames import frame_duration_ms, pcm_to_float32
from .language import LanguageArbiter, constrain_probabilities

log = logging.getLogger(__name__)

DEFAULT_REPO = "mlx-community/whisper-large-v3-turbo"

WHISPER_WINDOW_MS = 30_000
"""Whisper's fixed context. Longer audio needs the chunking loop in `mlx_whisper.transcribe`;
`EndpointingConfig.max_utterance_ms` is 30 s precisely so the fast path covers normal turns."""

TEMPERATURE_LADDER: tuple[float, ...] = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
COMPRESSION_RATIO_THRESHOLD = 2.4
LOGPROB_THRESHOLD = -1.0
"""Whisper's own decode-quality checks. A repetition loop compresses too well; a decode the
model is unsure of has a low average logprob. Both mean "retry hotter" — without this, a bad
30 ms of audio can produce the same phrase forty times and A.R.S answers it."""


class NoLanguageDistribution(RuntimeError):
    """Raised when the language-id pass returns nothing usable.

    Deliberately fatal rather than defaulted. During development this failure silently fell
    back to English and transcribed Romanian audio as "Good morning, I have got three
    messages from the bank" — fluent, confident and completely wrong. A loud failure is
    recoverable; a plausible mistranslation is not.
    """


class MlxWhisperEngine(AsrEngine):
    """Bilingual EN/RO streaming ASR on the Metal GPU."""

    name = "mlx-whisper"

    def __init__(
        self,
        *,
        repo: str = DEFAULT_REPO,
        model: str | None = None,
        dtype: str = "float16",
        model_dir: Path | str = "./models/asr",
        partial_interval_ms: float = 480.0,
        arbiter: LanguageArbiter | None = None,
        temperature_fallback: bool = True,
    ) -> None:
        # `model` is the shared `VoiceConfig.asr_model` name ("large-v3-turbo"); it selects an
        # mlx-community repo so one setting drives both backends.
        self.repo = repo if model is None else _repo_for(model, repo)
        self.dtype_name = dtype
        self.model_dir = Path(model_dir)
        self.partial_interval_ms = partial_interval_ms
        self.arbiter = arbiter or LanguageArbiter()
        self.temperature_fallback = temperature_fallback

        self._model = None
        self._mx = None
        self._audio = None
        self._decoding = None
        self._load_lock = asyncio.Lock()
        self._speech_active = True
        self.partials_skipped = 0
        self._partial_running = 0
        self.finals_contended = 0
        """Finals that began while a partial decode was still on the GPU. Both land on the
        same device, so an overlap roughly doubles the final's latency — and the final is the
        one on the budget. If this climbs, lengthen `partial_interval_ms`."""

    @property
    def supported_languages(self) -> tuple[Language, ...]:
        return SUPPORTED_LANGUAGES

    @property
    def loaded(self) -> bool:
        return self._model is not None

    # ------------------------------------------------------------------ lifecycle

    async def load(self) -> None:
        """Load the weights and compile the Metal kernels.

        Call at startup. The first decode after a cold load is ~500 ms against ~130 ms warm;
        paying that inside the first turn spends the entire ASR budget five times over.
        """
        if self._model is not None:
            return
        async with self._load_lock:
            if self._model is not None:
                return
            self._model = await asyncio.to_thread(self._load_blocking)

    warm_up = load

    def _load_blocking(self):
        try:
            import mlx.core as mx
            from mlx_whisper import audio as mlx_audio
            from mlx_whisper import decoding as mlx_decoding
            from mlx_whisper.transcribe import ModelHolder
        except ImportError as exc:
            raise RuntimeError(
                "MlxWhisperEngine needs the 'asr-mlx' extra on Apple Silicon: "
                "uv pip install -e 'services/voice[asr-mlx]'. "
                "Use ARS_ASR_BACKEND=faster-whisper elsewhere."
            ) from exc

        self._mx, self._audio, self._decoding = mx, mlx_audio, mlx_decoding
        dtype = getattr(mx, self.dtype_name, mx.float16)
        log.info("loading mlx-whisper %s dtype=%s", self.repo, self.dtype_name)
        # ModelHolder is mlx-whisper's own process-wide cache. Going through it means the
        # `transcribe()` fallback path reuses this instance instead of loading a second
        # copy of a 1.6 GB model.
        model = ModelHolder.get_model(self.repo, dtype)
        self._model = model
        # Kernel compilation, off the hot path. Warmed on a *representative* utterance in
        # both languages rather than a second of silence: MLX specialises kernels by shape,
        # and priming on silence decodes almost no tokens, so the first real turn was still
        # paying for compilation. Two extra decodes at startup, ~300 ms, once.
        primer = _priming_audio()
        for language in SUPPORTED_LANGUAGES:
            self._decode_window(primer, language)
        return model

    def set_speech_active(self, active: bool) -> None:
        """Told by the pipeline whether the user is still speaking.

        Optional and best-effort — the engine works without it. When the endpointer enters
        trailing silence there is no point spending 100 ms of GPU on another partial: the
        final decode is about to be needed and a partial in flight would land on top of it,
        inside the budgeted row. Partials resume when speech does.
        """
        self._speech_active = active

    # ------------------------------------------------------------------ interface

    async def transcribe(
        self,
        frames: AsyncIterator[AudioFrame],
        *,
        language: Language | None = None,
    ) -> AsyncIterator[Transcript]:
        await self.load()
        buffer = bytearray()
        audio_ms = 0.0
        next_partial_at = self.partial_interval_ms
        partial_task: asyncio.Task | None = None
        last_partial = ""

        try:
            async for frame in frames:
                buffer += frame.pcm
                audio_ms += frame_duration_ms(frame)

                if partial_task is not None and partial_task.done():
                    text = _result_or_empty(partial_task)
                    partial_task = None
                    if text and text != last_partial:
                        last_partial = text
                        yield Transcript(
                            text=text,
                            language=language or self.arbiter.current,
                            # Partials make no language claim: whisper's detection on half an
                            # utterance is close to a coin flip and the UI must not flicker
                            # between languages while the user is still talking.
                            language_confidence=0.0,
                            is_final=False,
                            audio_duration_ms=int(audio_ms),
                        )

                if audio_ms >= next_partial_at and partial_task is None:
                    next_partial_at = audio_ms + self.partial_interval_ms
                    if not self._speech_active:
                        self.partials_skipped += 1
                    else:
                        snapshot = bytes(buffer)
                        partial_task = asyncio.create_task(
                            asyncio.to_thread(
                                self._decode_partial, snapshot, language or self.arbiter.current
                            )
                        )
        finally:
            if partial_task is not None:
                # Cancels the awaitable, not the thread: MLX work already dispatched runs to
                # completion. Its result is dropped.
                partial_task.cancel()

        if self._partial_running:
            self.finals_contended += 1
            log.debug("final decode starting with %d partial(s) still on the GPU",
                      self._partial_running)
        yield await asyncio.to_thread(self._decode_final, bytes(buffer), language, audio_ms)

    # ------------------------------------------------------------------ decoding

    def _mel(self, audio: np.ndarray):
        mx, mxa = self._mx, self._audio
        window = mxa.pad_or_trim(mx.array(audio), mxa.N_SAMPLES)
        return mxa.log_mel_spectrogram(window, n_mels=self._model.dims.n_mels)

    def _options(self, language: Language, temperature: float):
        return self._decoding.DecodingOptions(
            language=language.value,
            temperature=temperature,
            without_timestamps=True,
            fp16=self.dtype_name == "float16",
        )

    def _decode_once(self, source, language: Language, temperature: float):
        result = self._decoding.decode(self._model, source, self._options(language, temperature))
        return result[0] if isinstance(result, list) else result

    def _decode_checked(self, source, language: Language):
        """Whisper's temperature ladder: retry hotter on a decode that looks degenerate."""
        result = self._decode_once(source, language, TEMPERATURE_LADDER[0])
        if not self.temperature_fallback:
            return result
        for temperature in TEMPERATURE_LADDER[1:]:
            degenerate = (
                result.compression_ratio > COMPRESSION_RATIO_THRESHOLD
                or result.avg_logprob < LOGPROB_THRESHOLD
            )
            if not degenerate:
                break
            log.debug(
                "mlx decode looked degenerate (compression=%.2f logprob=%.2f); retrying at %.1f",
                result.compression_ratio, result.avg_logprob, temperature,
            )
            result = self._decode_once(source, language, temperature)
        return result

    def _decode_window(self, audio: np.ndarray, language: Language):
        return self._decode_checked(self._mel(audio), language)

    def _detect(self, mel) -> tuple[Language, float, object]:
        """One encoder pass: returns the constrained language, its confidence, and the audio
        features so the transcription decode does not have to encode again."""
        result = self._decoding.decode(
            self._model,
            mel,
            self._decoding.DecodingOptions(
                task="lang_id", language=None, fp16=self.dtype_name == "float16"
            ),
        )
        result = result[0] if isinstance(result, list) else result
        probabilities = result.language_probs
        if not probabilities:
            raise NoLanguageDistribution(
                f"{self.repo} returned no language distribution; refusing to guess a language"
            )
        detected, confidence, scores = constrain_probabilities(
            probabilities, self.supported_languages
        )
        return detected, confidence, scores, result.audio_features

    def _decode_partial(self, pcm: bytes, language: Language) -> str:
        self._partial_running += 1
        try:
            return self._decode_partial_inner(pcm, language)
        finally:
            self._partial_running -= 1

    def _decode_partial_inner(self, pcm: bytes, language: Language) -> str:
        if not pcm:
            return ""
        audio = pcm_to_float32(pcm)
        if audio.size == 0:
            return ""
        return self._decode_window(_last_window(audio), language).text.strip()

    def _decode_final(
        self, pcm: bytes, language: Language | None, audio_ms: float
    ) -> Transcript:
        audio = pcm_to_float32(pcm)
        if audio.size == 0:
            return Transcript(
                text="",
                language=language or self.arbiter.current,
                language_confidence=0.0,
                is_final=True,
                audio_duration_ms=int(audio_ms),
            )

        long_form = audio.size > SAMPLE_RATE_HZ * WHISPER_WINDOW_MS // 1000

        if language is not None:
            resolved, confidence = language, 1.0
            text = self._transcribe_text(audio, resolved, long_form=long_form, features=None)
        else:
            # Language id runs on the first 30 s: it is a property of the speaker, not of the
            # tail of a long utterance, and running it on the whole thing would cost an extra
            # encoder pass per chunk for no gain.
            detected, confidence, scores, features = self._detect(self._mel(audio))
            text = self._transcribe_text(
                audio, detected, long_form=long_form, features=None if long_form else features
            )
            decision = self.arbiter.decide(detected, confidence, text=text, scores=scores)
            if decision.language is not detected:
                log.info("language arbiter: %s", decision.reason)
                # Re-decode in the language the arbiter kept. Free of an encoder pass on the
                # fast path, and it only happens on a short ambiguous utterance.
                text = self._transcribe_text(
                    audio,
                    decision.language,
                    long_form=long_form,
                    features=None if long_form else features,
                )
            resolved, confidence = decision.language, decision.confidence

        return Transcript(
            text=text,
            language=resolved,
            language_confidence=min(max(confidence, 0.0), 1.0),
            is_final=True,
            segments=(
                (
                    TranscriptSegment(
                        text=text, start_ms=0, end_ms=int(audio_ms), confidence=None
                    ),
                )
                if text
                else ()
            ),
            # One segment covering the utterance. `without_timestamps=True` is what makes the
            # decode fast; inventing per-word boundaries from it would be a fabrication, and
            # nothing downstream needs them. Ask for `mlx_whisper.transcribe` if you need real
            # timestamps and can afford the extra pass.
            audio_duration_ms=int(audio_ms),
        )

    def _transcribe_text(
        self, audio: np.ndarray, language: Language, *, long_form: bool, features
    ) -> str:
        if long_form:
            import mlx_whisper

            result = mlx_whisper.transcribe(
                audio,
                path_or_hf_repo=self.repo,
                language=language.value,
                temperature=TEMPERATURE_LADDER,
                condition_on_previous_text=False,
                fp16=self.dtype_name == "float16",
                verbose=None,
            )
            return str(result.get("text", "")).strip()
        source = features if features is not None else self._mel(audio)
        return self._decode_checked(source, language).text.strip()


def _priming_audio(duration_ms: float = 4_000.0) -> np.ndarray:
    """A few seconds of speech-shaped audio for warm-up. Not speech, but it produces a
    realistic number of decode steps, which is what the silence primer did not."""
    from ..audio.synth import speech_like_pcm

    return pcm_to_float32(speech_like_pcm(duration_ms, amplitude_dbfs=-24.0, seed=7))


def _last_window(audio: np.ndarray) -> np.ndarray:
    """Whisper sees 30 s. For a partial on a longer buffer, the recent audio is the part the
    user is waiting to see on screen."""
    limit = SAMPLE_RATE_HZ * WHISPER_WINDOW_MS // 1000
    return audio[-limit:] if audio.size > limit else audio


def _result_or_empty(task: asyncio.Task) -> str:
    try:
        return task.result()
    except (asyncio.CancelledError, Exception):
        log.debug("partial decode failed", exc_info=True)
        return ""


def _repo_for(model: str, fallback: str) -> str:
    """Map the shared `VoiceConfig.asr_model` name onto an mlx-community repo."""
    known = {
        "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
        "large-v3": "mlx-community/whisper-large-v3-mlx",
        "medium": "mlx-community/whisper-medium-mlx",
        "small": "mlx-community/whisper-small-mlx",
        "base": "mlx-community/whisper-base-mlx",
        "tiny": "mlx-community/whisper-tiny",
    }
    if model in known:
        return known[model]
    if "/" in model:
        return model
    log.warning("no mlx repo mapping for asr_model=%r; using %s", model, fallback)
    return fallback
