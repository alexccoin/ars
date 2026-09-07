"""Piper backend — the only place piper may be imported.

Piper has genuinely good Romanian voices (ro_RO-mihai-medium), which is most of why it is
the default: a bilingual assistant whose Romanian half sounds like a robot from 2004 is not
bilingual in any sense the user cares about.

Two things this class has to get right:

* **First audio at the first sentence boundary.** Piper synthesises a sentence at a time
  anyway, so the streaming wrapper feeds it sentences as they complete and the first one is
  on the wire while the LLM is still writing the second.
* **Cancel must stop generation.** Piper's inference is blocking C++ in a worker thread; a
  Python `Task.cancel()` cannot interrupt it. So the loop checks a cancel flag between
  chunks *and* between sentences, and the subprocess backend gets killed outright. Worst
  case one in-flight chunk is wasted, bounded by `chunk_ms`.

Two more things it has to get right, added when the catalogue arrived:

* **`SynthesisRequest.voice` is honoured.** It used to be ignored — the engine knew one
  voice per language and nothing else — which meant `speaker_id` was never set and the
  four multi-speaker models were 150 voices nobody could reach. `_select` turns the
  protocol string into a model, a speaker id and a resample ratio; `catalogue.py` is the
  list of names it accepts.
* **Voices load lazily and are evicted.** Each medium voice is 95-110 MB resident and
  ~315 ms to load, so holding thirty-one of them is 3 GB and ten seconds of startup.
  `warm_up()` loads exactly the active voice per language and *pins* it; everything else
  is loaded on first use and evicted least-recently-used. Pinning is the part that matters:
  an LRU that can evict the default voice re-loads it mid-turn and blows the 120 ms budget
  ~315 ms wide open, on a turn the user did not do anything unusual on.

Written against piper-tts 1.8; the pre-1.3 `synthesize_stream_raw` path is still handled.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from ars_core import TtsEngine
from ars_protocol import (
    SAMPLE_RATE_HZ,
    SUPPORTED_LANGUAGES,
    Language,
    SynthesisChunk,
    SynthesisRequest,
)

from ..audio.frames import bytes_for_ms, float32_to_pcm
from ..audio.sources import resample_to_protocol_rate
from .catalogue import SPEAKER_SEPARATOR, VOICES_BY_NAME, VoiceProfile, parse_voice_spec
from .segmentation import split_sentences

log = logging.getLogger(__name__)


_PRIMING_SENTENCE: dict[Language, str] = {
    Language.EN: "Good morning. I found three new messages from the bank.",
    Language.RO: "Bună dimineața. Am găsit trei mesaje noi de la bancă.",
    Language.DE: "Guten Morgen. Ich habe drei neue Nachrichten von der Bank gefunden.",
}
"""One realistic sentence per voice, used to prime it. Same content in each language on
purpose: priming cost tracks sentence length and phoneme variety, so a short German
sentence here would make German look faster than it is."""


class UnknownVoiceError(ValueError):
    """Asked for a voice that is neither in the catalogue nor a model file on disk."""


@dataclass(frozen=True)
class VoiceSelection:
    """What a request actually resolved to. Everything Piper needs, and nothing it does not.

    Separated from `VoiceProfile` because the escape-hatch spec `model#speaker` produces one
    of these without there being a catalogue entry at all, and because the engine caches by
    `model` — two catalogue names that share a model share one resident 95 MB voice.
    """

    requested: str
    model: str
    speaker_id: int | None = None
    formant_k: float = 1.0

    @property
    def shifted(self) -> bool:
        return self.formant_k != 1.0


class PiperTtsEngine(TtsEngine):
    """Local neural TTS. EN, RO and DE; many named voices per model."""

    name = "piper"

    def __init__(
        self,
        *,
        voice_en: str = "en_US-amy-medium",
        voice_ro: str = "ro_RO-mihai-medium",
        voice_de: str = "de_DE-thorsten-medium",
        model_dir: Path | str = "./models/tts",
        chunk_ms: float = 120.0,
        first_sentence_max_chars: int = 140,
        length_scale: float | None = None,
        max_resident_voices: int = 5,
        strict_voices: bool = False,
    ) -> None:
        # A dict, not a chain of branches: a fourth language must be a row here, not
        # another `if` that some other call site forgets to grow.
        self._voices_by_language = {
            Language.EN: voice_en, Language.RO: voice_ro, Language.DE: voice_de,
        }
        self.model_dir = Path(model_dir)
        self.chunk_ms = chunk_ms
        self.first_sentence_max_chars = first_sentence_max_chars
        self.length_scale = length_scale
        self.max_resident_voices = max(max_resident_voices, len(self._voices_by_language) + 1)
        """Cap on loaded models. Five mediums is ~550 MB.

        Never below `pinned + 1`: the pinned defaults cannot be evicted, so a cap equal to
        the number of languages leaves no room for the voice the user actually asked for and
        every single turn in a character voice pays a fresh ~315 ms load. The budget test
        caught exactly that — 400 ms to first audio on voices that measure 55 ms warm."""

        self.strict_voices = strict_voices
        """False: an unknown or wrong-language voice logs, counts, and falls back to the
        language default, because a mispronounced reply beats a silent turn. True: it raises.
        A UI that lets the user pick a voice should run strict so a bad pick is visible at
        the point of picking rather than as a voice that quietly is not the one they chose.
        """

        self._voices: OrderedDict[str, object] = OrderedDict()
        """Loaded models, most-recently-used last. Keyed by *model*, not by voice name: the
        twelve English catalogue voices sitting inside `en_GB-vctk-medium` cost one load."""

        self._speaker_maps: dict[str, dict[str, int]] = {}
        self.voice_loads = 0
        self.voice_evictions = 0
        self.voice_fallbacks = 0
        """Telemetry. `voice_evictions` climbing during normal use means the cap is too small
        for how the user actually switches voices, and every eviction is a future ~315 ms."""

        self._load_lock = asyncio.Lock()
        self._cancel = threading.Event()
        """threading.Event, not asyncio.Event: it is read from the synthesis worker thread."""
        self.cancellations = 0

    # ------------------------------------------------------------------ interface

    def voice_for(self, language: Language) -> str:
        return self._voices_by_language[language]

    @property
    def resident_voices(self) -> tuple[str, ...]:
        """Loaded models, least-recently-used first. The next eviction is `[0]`."""
        return tuple(self._voices)

    @property
    def pinned_voices(self) -> frozenset[str]:
        """The active voice per language. Warm at startup and never evicted."""
        return frozenset(self._voices_by_language.values())

    def resolve(self, spec: str | None, language: Language) -> VoiceSelection:
        """Turn `SynthesisRequest.voice` into a model + speaker id. Raises on anything it
        cannot honour; `_select` is the forgiving wrapper the synthesis path uses.

        Accepted forms are documented on `catalogue.VoiceSpec`. Order matters: a catalogue
        name wins over a file of the same name, so renaming a model file cannot silently
        redirect a name the user picked.
        """
        parsed = parse_voice_spec(spec)
        name = parsed.voice
        if not name:
            default = self.voice_for(language)
            return VoiceSelection(requested=default, model=default)

        profile = VOICES_BY_NAME.get(name)
        if profile is not None:
            if profile.language is not language:
                raise UnknownVoiceError(
                    f"voice {name!r} speaks {profile.language.value}, "
                    f"but this turn is {language.value}"
                )
            return self._from_profile(profile)

        model, _, speaker = name.partition(SPEAKER_SEPARATOR)
        if not self._model_path(model).is_file():
            raise UnknownVoiceError(
                f"unknown voice {name!r}: not in the catalogue "
                f"({len(VOICES_BY_NAME)} names) and no {model}.onnx in {self.model_dir}"
            )
        speaker_id = self.speaker_id_for(model, speaker) if speaker else None
        return VoiceSelection(requested=name, model=model, speaker_id=speaker_id)

    def _from_profile(self, profile: VoiceProfile) -> VoiceSelection:
        return VoiceSelection(
            requested=profile.name,
            model=profile.model,
            speaker_id=profile.speaker_id,
            formant_k=profile.formant_k,
        )

    def _select(self, request: SynthesisRequest) -> VoiceSelection:
        """Resolution on the synthesis path: never fails the turn unless `strict_voices`."""
        try:
            return self.resolve(request.voice, request.language)
        except (UnknownVoiceError, ValueError):
            if self.strict_voices:
                raise
            self.voice_fallbacks += 1
            # WARNING, not debug: a voice the user picked and did not get is exactly the
            # kind of thing that is invisible in text logs and obvious in the audio.
            log.warning(
                "voice %r unusable for %s, falling back to %s",
                request.voice, request.language.value, self.voice_for(request.language),
            )
            default = self.voice_for(request.language)
            return VoiceSelection(requested=default, model=default)

    def speaker_id_for(self, model: str, speaker: str) -> int:
        """Look a speaker key up in the model's own sidecar JSON.

        Reading the 5 KB `.onnx.json` rather than the 77 MB `.onnx`: enumerating VCTK's 109
        speakers must not cost a model load. Catalogue entries carry their id inline and
        never come through here; this is for the `model#speaker` escape hatch.
        """
        mapping = self.speakers_in(model)
        try:
            return mapping[speaker]
        except KeyError:
            raise UnknownVoiceError(
                f"{model} has no speaker {speaker!r} ({len(mapping)} speakers, "
                f"e.g. {sorted(mapping)[:5]})"
            ) from None

    def speakers_in(self, model: str) -> dict[str, int]:
        """Every speaker in a model, name -> id. What a picker enumerates to get past the
        curated roster into all 109 VCTK voices."""
        if model not in self._speaker_maps:
            sidecar = self._model_path(model).with_suffix(".onnx.json")
            if not sidecar.is_file():
                raise UnknownVoiceError(f"no config for {model} at {sidecar}")
            config = json.loads(sidecar.read_text())
            self._speaker_maps[model] = dict(config.get("speaker_id_map") or {})
        return self._speaker_maps[model]

    async def cancel(self) -> None:
        self.cancellations += 1
        self._cancel.set()

    async def load(self, language: Language, voice: str | None = None) -> None:
        """Preload one voice. `voice` preloads a non-default one — a persona switch that
        knows what is coming next can pay the ~315 ms before the user asks a question
        instead of inside their turn."""
        await self._model(self.resolve(voice, language).model)

    async def warm_up(
        self, languages: Sequence[Language] = SUPPORTED_LANGUAGES
    ) -> dict[Language, float]:
        """Load and prime every voice A.R.S can speak in. Call at startup.

        Measured on this machine (M5 Max, piper-tts 1.8, medium voices):

        | | EN | RO |
        |---|---:|---:|
        | voice load | 355 ms | 355 ms |
        | first synthesis after load | 49 ms | 23 ms |
        | first synthesis, warm | 18 ms | 18 ms |

        The TTS budget is 120 ms to first audio. Warm, there is 100 ms of headroom; cold,
        the load alone is over three times the whole budget. And it is *per voice*, so a
        bilingual household misses the budget twice — once the first time it is spoken to in
        English and again the first time in Romanian. Preloading both is not an optimisation,
        it is the difference between meeting the budget and not.

        **What this deliberately does not do is preload the catalogue.** Thirty-one named
        voices at 95-110 MB and ~315 ms each is 3 GB and ten seconds; warm-up covers the one
        active voice per language and nothing else. Those are also the pinned ones, so no
        amount of voice-hopping afterwards can evict what the next ordinary turn needs.
        A persona switch pays the load once, which is fine — it is a deliberate user action,
        not a turn.
        """
        timings: dict[Language, float] = {}
        for language in languages:
            start = time.perf_counter()
            voice = await self._model(self.voice_for(language))
            await asyncio.to_thread(self._prime, voice, language)
            timings[language] = (time.perf_counter() - start) * 1000
            log.info("piper %s warm in %.0f ms", self.voice_for(language), timings[language])
        return timings

    def _prime(self, voice, language: Language) -> None:
        """One throwaway synthesis. Loading the ONNX graph is not the whole cost: the first
        inference allocates its arenas and runs espeak-ng phonemisation for the first time."""
        # A full sentence, not one word: ONNX Runtime allocates per input shape, and priming
        # on "Ready." left the first real reply paying for it — measured as a 115 ms
        # time-to-first-audio on turn one against 40-60 ms afterwards.
        text = _PRIMING_SENTENCE[language]
        default = self.voice_for(language)
        selection = VoiceSelection(requested=default, model=default)
        for _ in self._iter_piper_audio(voice, text, selection, speed=1.0):
            return

    async def synthesize(self, request: SynthesisRequest) -> AsyncIterator[SynthesisChunk]:
        self._cancel.clear()
        selection = self._select(request)
        voice = await self._model(selection.model)
        sentences = split_sentences(request.text, first_max_chars=self.first_sentence_max_chars)
        seq = 0
        for sentence in sentences:
            if self._cancel.is_set():
                return
            queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=8)
            loop = asyncio.get_running_loop()
            # Per-sentence stop flag, separate from the engine-wide cancel. A consumer that
            # simply stops reading — `break` out of the `async for`, or the streaming
            # synthesiser dropping the rest of a reply — closes this generator, and the
            # worker thread has to learn about it. Without this the thread spins for ever on
            # a full queue and the process will not exit. That leak was real and only showed
            # up with Piper's weights loaded.
            stop = threading.Event()
            worker = asyncio.create_task(
                asyncio.to_thread(
                    self._synthesize_sentence,
                    voice, sentence, selection, request.speed, queue, loop, stop,
                )
            )
            try:
                while True:
                    try:
                        # Never block for ever on the worker: if cancel lands while it is
                        # inside a blocking inference call its sentinel may never arrive, so
                        # barge-in is bounded by this poll rather than by the reply length.
                        pcm = await asyncio.wait_for(queue.get(), timeout=0.1)
                    except TimeoutError:
                        if self._cancel.is_set() or worker.done():
                            break
                        continue
                    if pcm is None:
                        break
                    if self._cancel.is_set():
                        break
                    yield SynthesisChunk(seq=seq, pcm=pcm, is_final=False)
                    seq += 1
            finally:
                stop.set()
                worker.cancel()
                _drain(queue)
        if self._cancel.is_set():
            return
        yield SynthesisChunk(seq=seq, pcm=b"", is_final=True)

    # ------------------------------------------------------------------ internals

    def _synthesis_config(self, length_scale: float, speaker_id: int | None):
        """`SynthesisRequest.speed` and the chosen speaker both have to reach Piper, or the
        protocol field and the multi-speaker models are decoration.

        length_scale is duration per phoneme, so it is the reciprocal of speed: 2.0x speed is
        length_scale 0.5. `speaker_id` has always existed on this dataclass and was never
        being set, which is the whole reason 150 English voices were unreachable.
        """
        try:
            from piper.config import SynthesisConfig
        except ImportError:  # pragma: no cover - older piper takes the raw_stream path
            return None
        return SynthesisConfig(length_scale=length_scale, speaker_id=speaker_id)

    def _model_path(self, name: str) -> Path:
        return self.model_dir / f"{name}.onnx"

    def _require_model_path(self, name: str) -> Path:
        candidate = self._model_path(name)
        if not candidate.is_file():
            raise FileNotFoundError(
                f"piper voice {name} not found at {candidate}. Run scripts/fetch_voice_models.sh"
            )
        return candidate

    async def _model(self, name: str):
        """Get a loaded model, loading it if needed and evicting to stay under the cap.

        The eviction only drops our reference. A synthesis already running holds the object
        itself, so evicting mid-turn is safe — it costs the *next* use of that voice a load,
        never a crash.
        """
        if name in self._voices:
            self._voices.move_to_end(name)
            return self._voices[name]
        async with self._load_lock:
            if name in self._voices:  # someone loaded it while we waited
                self._voices.move_to_end(name)
                return self._voices[name]
            started = time.perf_counter()
            voice = await self._load_model(name)
            self._voices[name] = voice
            self.voice_loads += 1
            log.info(
                "loaded piper voice %s in %.0f ms", name, (time.perf_counter() - started) * 1000
            )
            self._evict(keep=name)
            return voice

    async def _load_model(self, name: str):
        """The one place a model file becomes an object, and the seam the tests replace.

        Lazy loading, eviction and pinning are policy that has to be right whether or not
        77 MB of weights are on the disk — CI has none — so the policy lives in `_model` and
        the 315 ms of ONNX lives here, overridable.
        """
        try:
            from piper.voice import PiperVoice
        except ImportError as exc:
            raise RuntimeError(
                "PiperTtsEngine needs the 'tts' extra: uv pip install -e 'services/voice[tts]'"
            ) from exc
        path = self._require_model_path(name)
        log.info("loading piper voice %s", path)
        return await asyncio.to_thread(PiperVoice.load, str(path))

    def _evict(self, *, keep: str | None = None) -> None:
        """Least-recently-used out; the pinned defaults and the voice just loaded never.

        Without the pin, the third persona the user tries pushes out the voice their next
        ordinary sentence needs. Without `keep`, a full cache evicts the voice it has just
        spent 315 ms loading, before a single sample comes out of it.
        """
        protected = self.pinned_voices | ({keep} if keep else set())
        while len(self._voices) > self.max_resident_voices:
            victim = next((n for n in self._voices if n not in protected), None)
            if victim is None:  # everything resident is pinned; the cap yields, not the budget
                return
            del self._voices[victim]
            self.voice_evictions += 1
            log.info("evicted piper voice %s (%d resident)", victim, len(self._voices))

    def _synthesize_sentence(
        self,
        voice,
        sentence: str,
        selection: VoiceSelection,
        speed: float,
        queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
        stop: threading.Event,
    ) -> None:
        """Runs in a worker thread. Pushes protocol-rate PCM chunks onto `queue`."""
        try:
            for pcm in self._iter_piper_audio(voice, sentence, selection, speed):
                if self._stopping(stop):
                    break
                for chunk in _split(pcm, bytes_for_ms(self.chunk_ms)):
                    if self._stopping(stop) or not self._push(queue, loop, chunk, stop):
                        return
        except Exception:  # pragma: no cover - needs weights
            log.exception("piper synthesis failed")
        finally:
            # Best effort and never blocking. Routing the end-of-stream sentinel through
            # `_push` would drop it on exactly the path that needs it — `_push` gives up as
            # soon as the stop flag is set — and hang the consumer.
            loop.call_soon_threadsafe(_offer_sentinel, queue)

    def _stopping(self, stop: threading.Event) -> bool:
        return self._cancel.is_set() or stop.is_set()

    def _push(
        self,
        queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
        item,
        stop: threading.Event,
    ) -> bool:
        """Hand a chunk to the event loop without ever blocking forever.

        The consumer stops reading the instant a barge-in lands. A plain blocking put would
        strand this worker thread on a full queue for the lifetime of the process, and a few
        barge-ins later the thread pool is gone.
        """
        future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
        while not self._stopping(stop):
            try:
                future.result(timeout=0.05)
                return True
            except TimeoutError:
                continue
            except Exception:  # pragma: no cover - loop closed
                return False
        future.cancel()
        return False

    def _iter_piper_audio(
        self, voice, sentence: str, selection: VoiceSelection, speed: float
    ) -> Iterator[bytes]:
        """Bridge over piper's API drift, and resample to the protocol rate.

        Piper voices are 22.05 kHz; the pipeline is `SAMPLE_RATE_HZ`. The conversion happens
        here, at the backend edge, so no `SynthesisChunk` ever carries a rate other than the
        one declared in `AudioFormat`.

        **The formant shift is free because of that resample.** Telling the resampler the
        source was `22050·k` shifts pitch *and* formants by k — a physically different-sized
        speaker, not a slowed-down one — for no extra work, since the resample happens
        anyway. `length_scale` is multiplied by k to undo the duration change; that
        compensation is good to about 10%, because `length_scale` does not scale pauses
        linearly. Measured accuracy of the pitch shift itself: ~0.15 semitones
        (research/voices.md §3.3).
        """
        base = self.length_scale if self.length_scale is not None else 1.0 / speed
        length_scale = base * selection.formant_k
        source_rate = int(getattr(getattr(voice, "config", None), "sample_rate", SAMPLE_RATE_HZ))

        stream = getattr(voice, "synthesize", None)
        raw_stream = getattr(voice, "synthesize_stream_raw", None)
        if callable(raw_stream):  # piper-tts < 1.3
            kwargs = {} if selection.speaker_id is None else {"speaker_id": selection.speaker_id}
            for pcm in raw_stream(sentence, length_scale=length_scale, **kwargs):
                yield _to_protocol_rate(pcm, _pretend_rate(source_rate, selection.formant_k))
            return
        if callable(stream):  # piper-tts >= 1.3 yields AudioChunk objects
            config = self._synthesis_config(length_scale, selection.speaker_id)
            for item in stream(sentence, config):
                pcm = getattr(item, "audio_int16_bytes", None)
                if pcm is None:
                    floats = getattr(item, "audio_float_array", None)
                    if floats is None:
                        continue
                    pcm = float32_to_pcm(np.asarray(floats, dtype=np.float32))
                rate = int(getattr(item, "sample_rate", source_rate))
                yield _to_protocol_rate(pcm, _pretend_rate(rate, selection.formant_k))
            return
        raise RuntimeError("unsupported piper-tts version: no synthesize/synthesize_stream_raw")


def _pretend_rate(source_rate: int, formant_k: float) -> int:
    """The lie that buys the pitch/formant shift. k > 1 claims a faster source, so the
    resampler produces fewer output samples per input sample and everything moves up."""
    return round(source_rate * formant_k) if formant_k != 1.0 else source_rate


def _to_protocol_rate(pcm: bytes, source_rate: int) -> bytes:
    if source_rate == SAMPLE_RATE_HZ or not pcm:
        return pcm
    samples = np.frombuffer(pcm, dtype="<i2")
    return resample_to_protocol_rate(samples, source_rate).astype("<i2").tobytes()


def _offer_sentinel(queue: asyncio.Queue) -> None:
    """Put the end-of-stream marker even if the queue is full: drop one chunk to make room.
    A dropped chunk on a finished or abandoned sentence is inaudible; a missing sentinel
    hangs the turn."""
    try:
        queue.put_nowait(None)
    except asyncio.QueueFull:
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            queue.put_nowait(None)


def _drain(queue: asyncio.Queue) -> None:
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return


def _split(pcm: bytes, size: int) -> Iterator[bytes]:
    for offset in range(0, len(pcm), size):
        yield pcm[offset : offset + size]
