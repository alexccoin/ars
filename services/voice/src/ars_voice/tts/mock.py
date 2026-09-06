"""Mock TTS — streams real PCM, honours cancel, models generation cost.

The point of a mock TTS in this pipeline is barge-in. Cancellation is where voice
assistants break, and it only breaks under conditions a trivial stub never creates:
generation still running, chunks in flight, a sink mid-write. So this engine actually
generates audio, takes time doing it (`realtime_factor`), and records how many chunks it
produced *after* cancel was requested — which is what the barge-in test asserts on.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from ars_core import TtsEngine
from ars_protocol import Language, SynthesisChunk, SynthesisRequest

from ..audio.frames import bytes_for_ms
from ..audio.synth import formants_for, speech_like_pcm
from .segmentation import SentenceStreamer, split_sentences

MS_PER_CHARACTER = 62.0
"""Speaking rate used to size the synthetic audio: ~16 characters/second, which is close to
unhurried conversational speech in both EN and RO."""


_MOCK_F0_HZ: dict[Language, float] = {
    Language.EN: 124.0, Language.RO: 112.0, Language.DE: 118.0,
}
"""Distinct per language so a test can tell the voices apart from the audio alone. The
values are arbitrary; only their being different carries meaning."""


class MockTtsEngine(TtsEngine):
    """Deterministic, cancellable, bilingual."""

    name = "mock"

    def __init__(
        self,
        *,
        chunk_ms: float = 120.0,
        realtime_factor: float = 12.0,
        first_chunk_latency_ms: float = 15.0,
        voice_en: str = "mock_en",
        voice_ro: str = "mock_ro",
        voice_de: str = "mock_de",
        first_sentence_max_chars: int = 140,
    ) -> None:
        self.chunk_ms = chunk_ms
        self.realtime_factor = max(realtime_factor, 0.01)
        self.first_chunk_latency_ms = first_chunk_latency_ms
        self._voices_by_language = {
            Language.EN: voice_en, Language.RO: voice_ro, Language.DE: voice_de,
        }
        self.first_sentence_max_chars = first_sentence_max_chars

        self._cancelled = asyncio.Event()
        self.chunks_emitted = 0
        self.chunks_after_cancel = 0
        self.cancellations = 0
        self.interrupted = 0
        """Syntheses abandoned mid-stream because cancel() landed. Survives across calls, so
        a test can prove generation stopped even after a later turn resets the cancel flag."""
        self.completed = False
        self.last_request: SynthesisRequest | None = None

    def voice_for(self, language: Language) -> str:
        return self._voices_by_language[language]

    async def cancel(self) -> None:
        """Stops generation, not just playback. The synthesis loop checks this event before
        every chunk and before every sentence, so at worst one already-generated chunk is
        in flight — bounded by `chunk_ms`, not by the length of the reply."""
        self.cancellations += 1
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    async def synthesize(self, request: SynthesisRequest) -> AsyncIterator[SynthesisChunk]:
        self._cancelled.clear()
        self.last_request = request
        self.completed = False
        seq = 0
        sentences = split_sentences(request.text, first_max_chars=self.first_sentence_max_chars)
        if not sentences:
            self.completed = True
            yield SynthesisChunk(seq=0, pcm=b"", is_final=True)
            return

        try:
            for index, sentence in enumerate(sentences):
                if self._cancelled.is_set():
                    return
                # Generation cost per sentence, paid before its first chunk. Emitting at the
                # first sentence boundary is what keeps this off the critical path for
                # sentences 2..n.
                await asyncio.sleep(self.first_chunk_latency_ms / 1000.0)
                async for chunk in self._sentence_chunks(sentence, request, seq, index):
                    if self._cancelled.is_set():
                        self.chunks_after_cancel += 1
                        return
                    seq = chunk.seq + 1
                    self.chunks_emitted += 1
                    yield chunk

            if self._cancelled.is_set():
                return
            self.completed = True
            yield SynthesisChunk(seq=seq, pcm=b"", is_final=True)
        finally:
            # Counts abandonment however it arrives: our own cancel flag, or the consumer
            # closing the generator (which raises GeneratorExit at the yield and skips every
            # check below it — the reason this lives in a finally and not after the loop).
            if not self.completed:
                self.interrupted += 1

    async def _sentence_chunks(
        self, sentence: str, request: SynthesisRequest, start_seq: int, sentence_index: int
    ) -> AsyncIterator[SynthesisChunk]:
        duration_ms = max(self.chunk_ms, len(sentence) * MS_PER_CHARACTER / request.speed)
        pcm = speech_like_pcm(
            duration_ms,
            f0_hz=_MOCK_F0_HZ[request.language],
            formants=formants_for(request.language.value),
            seed=abs(hash((sentence, request.language.value))) % 2**31,
        )
        chunk_bytes = bytes_for_ms(self.chunk_ms)
        seq = start_seq
        for offset in range(0, len(pcm), chunk_bytes):
            if self._cancelled.is_set():
                return
            # Generation is faster than realtime, like a real synthesiser: the sink, not the
            # model, is what takes wall-clock time to play a reply.
            await asyncio.sleep(self.chunk_ms / 1000.0 / self.realtime_factor)
            yield SynthesisChunk(seq=seq, pcm=pcm[offset : offset + chunk_bytes], is_final=False)
            seq += 1


class NullTtsEngine(TtsEngine):
    """Emits nothing but a final marker. For text-only clients and for tests that care
    about the state machine rather than the audio."""

    name = "null"

    def __init__(self) -> None:
        self.cancellations = 0
        self.requests: list[SynthesisRequest] = []

    def voice_for(self, language: Language) -> str:
        return f"null_{language.value}"

    async def cancel(self) -> None:
        self.cancellations += 1

    async def synthesize(self, request: SynthesisRequest) -> AsyncIterator[SynthesisChunk]:
        self.requests.append(request)
        yield SynthesisChunk(seq=0, pcm=b"", is_final=True)


__all__ = ["MS_PER_CHARACTER", "MockTtsEngine", "NullTtsEngine", "SentenceStreamer"]
