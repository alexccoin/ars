"""Turn a stream of reply text into a stream of audio, starting at the first sentence.

This wrapper is what actually buys the TTS budget. The engine synthesises a sentence at a
time; the reply arrives a token at a time; the job here is to hand the engine sentence one
the instant it is complete and to keep the sequence numbers monotonic across sentences so
the sink can detect a gap.

Cancellation is first-class: `cancel()` reaches the engine (which stops generating) *and*
stops pulling from the reply stream, which is what makes barge-in reach the compute request
instead of politely muting the speaker while the machine keeps working.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from ars_core import TtsEngine
from ars_protocol import Language, SynthesisChunk, SynthesisRequest

from .segmentation import SentenceStreamer


class StreamingSynthesizer:
    """Sentence-at-a-time synthesis over a token stream."""

    def __init__(
        self,
        engine: TtsEngine,
        *,
        first_sentence_max_chars: int = 140,
        speed: float = 1.0,
    ) -> None:
        self.engine = engine
        self.speed = speed
        self._segmenter = SentenceStreamer(first_max_chars=first_sentence_max_chars)
        self._cancelled = asyncio.Event()
        self.sentences_spoken = 0
        self.chunks_emitted = 0

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    async def cancel(self) -> None:
        self._cancelled.set()
        await self.engine.cancel()

    def reset(self) -> None:
        self._cancelled.clear()
        self._segmenter.reset()
        self.sentences_spoken = 0
        self.chunks_emitted = 0

    async def stream(
        self, deltas: AsyncIterator[str], *, language: Language, voice: str | None = None
    ) -> AsyncIterator[SynthesisChunk]:
        self.reset()
        seq = 0
        pending: list[str] = []

        async for delta in deltas:
            if self._cancelled.is_set():
                return
            for sentence in self._segmenter.push(delta):
                pending.append(sentence)
            while pending:
                if self._cancelled.is_set():
                    return
                async for chunk in self._speak(pending.pop(0), language, voice, seq):
                    seq = chunk.seq + 1
                    yield chunk

        if self._cancelled.is_set():
            return
        for sentence in [*pending, *self._segmenter.flush()]:
            if self._cancelled.is_set():
                return
            async for chunk in self._speak(sentence, language, voice, seq):
                seq = chunk.seq + 1
                yield chunk

        if not self._cancelled.is_set():
            yield SynthesisChunk(seq=seq, pcm=b"", is_final=True)

    async def speak(
        self, text: str, *, language: Language, voice: str | None = None
    ) -> AsyncIterator[SynthesisChunk]:
        """Non-streaming entry point, for fillers and guard prompts whose text is known up
        front (the 600 ms tool-call acknowledgement is one of these)."""

        async def once() -> AsyncIterator[str]:
            yield text

        async for chunk in self.stream(once(), language=language, voice=voice):
            yield chunk

    async def _speak(
        self, sentence: str, language: Language, voice: str | None, start_seq: int
    ) -> AsyncIterator[SynthesisChunk]:
        if not sentence.strip():
            return
        request = SynthesisRequest(
            text=sentence,
            language=language,
            voice=voice or self.engine.voice_for(language),
            speed=self.speed,
        )
        self.sentences_spoken += 1
        seq = start_seq
        async for chunk in self.engine.synthesize(request):
            if self._cancelled.is_set():
                return
            if chunk.is_final:
                # Per-sentence finals are internal; only the end of the whole reply is final
                # on the wire, or the client tears the playback stream down after sentence 1.
                continue
            self.chunks_emitted += 1
            yield SynthesisChunk(seq=seq, pcm=chunk.pcm, is_final=False)
            seq += 1
