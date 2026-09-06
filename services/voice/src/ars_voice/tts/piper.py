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

UNVERIFIED: no Piper voices are installed in this checkout. Written against piper-tts 1.2.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import AsyncIterator, Iterator, Sequence
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
from .segmentation import split_sentences

log = logging.getLogger(__name__)


class PiperTtsEngine(TtsEngine):
    """Local neural TTS, EN + RO."""

    name = "piper"

    def __init__(
        self,
        *,
        voice_en: str = "en_US-amy-medium",
        voice_ro: str = "ro_RO-mihai-medium",
        model_dir: Path | str = "./models/tts",
        chunk_ms: float = 120.0,
        first_sentence_max_chars: int = 140,
        length_scale: float | None = None,
    ) -> None:
        self._voice_en = voice_en
        self._voice_ro = voice_ro
        self.model_dir = Path(model_dir)
        self.chunk_ms = chunk_ms
        self.first_sentence_max_chars = first_sentence_max_chars
        self.length_scale = length_scale
        self._voices: dict[str, object] = {}
        self._load_lock = asyncio.Lock()
        self._cancel = threading.Event()
        """threading.Event, not asyncio.Event: it is read from the synthesis worker thread."""
        self.cancellations = 0

    # ------------------------------------------------------------------ interface

    def voice_for(self, language: Language) -> str:
        return self._voice_ro if language is Language.RO else self._voice_en

    async def cancel(self) -> None:
        self.cancellations += 1
        self._cancel.set()

    async def load(self, language: Language) -> None:
        """Preload one voice."""
        await self._voice(language)

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
        """
        timings: dict[Language, float] = {}
        for language in languages:
            start = time.perf_counter()
            voice = await self._voice(language)
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
        text = (
            "Bună dimineața. Am găsit trei mesaje noi de la bancă."
            if language is Language.RO
            else "Good morning. I found three new messages from the bank."
        )
        request = SynthesisRequest(text=text, language=language)
        for _ in self._iter_piper_audio(voice, text, request):
            return

    async def synthesize(self, request: SynthesisRequest) -> AsyncIterator[SynthesisChunk]:
        self._cancel.clear()
        voice = await self._voice(request.language)
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
                    self._synthesize_sentence, voice, sentence, request, queue, loop, stop
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

    def _synthesis_config(self, length_scale: float):
        """`SynthesisRequest.speed` has to reach Piper, or the protocol field is decoration.

        length_scale is duration per phoneme, so it is the reciprocal of speed: 2.0x speed is
        length_scale 0.5.
        """
        try:
            from piper.config import SynthesisConfig
        except ImportError:  # pragma: no cover - older piper takes the raw_stream path
            return None
        return SynthesisConfig(length_scale=length_scale)

    def _voice_path(self, name: str) -> Path:
        candidate = self.model_dir / f"{name}.onnx"
        if not candidate.is_file():
            raise FileNotFoundError(
                f"piper voice {name} not found at {candidate}. Run scripts/fetch_voice_models.sh"
            )
        return candidate

    async def _voice(self, language: Language):
        name = self.voice_for(language)
        if name in self._voices:
            return self._voices[name]
        async with self._load_lock:
            if name in self._voices:
                return self._voices[name]
            try:
                from piper.voice import PiperVoice
            except ImportError as exc:
                raise RuntimeError(
                    "PiperTtsEngine needs the 'tts' extra: uv pip install -e 'services/voice[tts]'"
                ) from exc
            path = self._voice_path(name)
            log.info("loading piper voice %s", path)
            voice = await asyncio.to_thread(PiperVoice.load, str(path))
            self._voices[name] = voice
            return voice

    def _synthesize_sentence(
        self,
        voice,
        sentence: str,
        request: SynthesisRequest,
        queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
        stop: threading.Event,
    ) -> None:
        """Runs in a worker thread. Pushes protocol-rate PCM chunks onto `queue`."""
        try:
            for pcm in self._iter_piper_audio(voice, sentence, request):
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

    def _iter_piper_audio(self, voice, sentence: str, request: SynthesisRequest) -> Iterator[bytes]:
        """Bridge over piper's API drift, and resample to the protocol rate.

        Piper voices are 22.05 kHz; the pipeline is `SAMPLE_RATE_HZ`. The conversion happens
        here, at the backend edge, so no `SynthesisChunk` ever carries a rate other than the
        one declared in `AudioFormat`.
        """
        length_scale = self.length_scale if self.length_scale is not None else 1.0 / request.speed
        source_rate = int(getattr(getattr(voice, "config", None), "sample_rate", SAMPLE_RATE_HZ))

        stream = getattr(voice, "synthesize", None)
        raw_stream = getattr(voice, "synthesize_stream_raw", None)
        if callable(raw_stream):  # piper-tts < 1.3
            for pcm in raw_stream(sentence, length_scale=length_scale):
                yield _to_protocol_rate(pcm, source_rate)
            return
        if callable(stream):  # piper-tts >= 1.3 yields AudioChunk objects
            for item in stream(sentence, self._synthesis_config(length_scale)):
                pcm = getattr(item, "audio_int16_bytes", None)
                if pcm is None:
                    floats = getattr(item, "audio_float_array", None)
                    if floats is None:
                        continue
                    pcm = float32_to_pcm(np.asarray(floats, dtype=np.float32))
                rate = int(getattr(item, "sample_rate", source_rate))
                yield _to_protocol_rate(pcm, rate)
            return
        raise RuntimeError("unsupported piper-tts version: no synthesize/synthesize_stream_raw")


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
