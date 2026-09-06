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
import logging
import threading
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import numpy as np
from ars_core import TtsEngine
from ars_protocol import SAMPLE_RATE_HZ, Language, SynthesisChunk, SynthesisRequest

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
        """Preload a voice. Piper models are ~60 MB and load in ~100-200 ms; doing it inside
        the first turn costs more than the entire TTS budget."""
        await self._voice(language)

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
            worker = asyncio.create_task(
                asyncio.to_thread(self._synthesize_sentence, voice, sentence, request, queue, loop)
            )
            try:
                while True:
                    pcm = await queue.get()
                    if pcm is None:
                        break
                    if self._cancel.is_set():
                        break
                    yield SynthesisChunk(seq=seq, pcm=pcm, is_final=False)
                    seq += 1
            finally:
                # The worker observes `self._cancel` and exits on its own; awaiting the
                # thread here would block the barge-in path on an in-flight inference.
                worker.cancel()
                _drain(queue)
        if self._cancel.is_set():
            return
        yield SynthesisChunk(seq=seq, pcm=b"", is_final=True)

    # ------------------------------------------------------------------ internals

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
    ) -> None:
        """Runs in a worker thread. Pushes protocol-rate PCM chunks onto `queue`."""
        try:
            for pcm in self._iter_piper_audio(voice, sentence, request):
                if self._cancel.is_set():
                    break
                for chunk in _split(pcm, bytes_for_ms(self.chunk_ms)):
                    if self._cancel.is_set() or not self._push(queue, loop, chunk):
                        return
        except Exception:  # pragma: no cover - needs weights
            log.exception("piper synthesis failed")
        finally:
            self._push(queue, loop, None)

    def _push(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop, item) -> bool:
        """Hand a chunk to the event loop without ever blocking forever.

        The consumer stops reading the instant a barge-in lands. A plain blocking put would
        strand this worker thread on a full queue for the lifetime of the process, and a few
        barge-ins later the thread pool is gone.
        """
        future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
        while not self._cancel.is_set():
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
            for item in stream(sentence):
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


def _drain(queue: asyncio.Queue) -> None:
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return


def _split(pcm: bytes, size: int) -> Iterator[bytes]:
    for offset in range(0, len(pcm), size):
        yield pcm[offset : offset + size]
