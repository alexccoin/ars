"""`CharacterTtsEngine` — robot and alien voices as a wrapper over any TTS engine.

This is deliberately *not* inside `PiperTtsEngine`. A wrapper that maps one engine's
`SynthesisChunk` stream into another:

* keeps CLAUDE.md #2 intact — no vendor SDK is involved, so the effect works over Piper
  today and over whatever replaces it later, and it is testable with `MockTtsEngine` and no
  weights at all;
* keeps `cancel()` honest — the wrapper cancels by *stopping pulling*, so barge-in reaches
  the real generator instead of muting a pipeline that is still working;
* is language-independent — `robot_dalek` exists in Romanian on the same day it exists in
  English, which matters enormously here, because Romanian has one voice and no character
  model will ever be trained for it.

The one part of the character register that does *not* live here is the pitch/formant shift
`k`: it has to reach Piper's resample and `length_scale`, so it is a property of a
`VoiceProfile` and is applied by the backend. See `catalogue.VoiceProfile.formant_k`.

Cost, measured: 0.08-1.01 ms per 120 ms chunk. Against a 120 ms time-to-first-audio budget
of which a medium Piper voice spends 57-95 ms, the entire sci-fi register is rounding error.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence

from ars_core import TtsEngine
from ars_protocol import SUPPORTED_LANGUAGES, Language, SynthesisChunk, SynthesisRequest

from .catalogue import CHARACTER_SEPARATOR, CHARACTERS, parse_voice_spec
from .dsp import CharacterChain

log = logging.getLogger(__name__)


class CharacterTtsEngine(TtsEngine):
    """Applies a character effect to another engine's audio.

    The character comes from the request: `SynthesisRequest.voice` of `"robot_dalek"` uses
    the language's default voice, `"robot_dalek/alan"` uses a specific one, and a plain
    voice name passes straight through with no processing at all. A `character=` given to
    the constructor is the default when the request does not name one — that is how a
    "persona" is configured once rather than threaded through every call site.
    """

    name = "character"

    def __init__(self, inner: TtsEngine, *, character: str | None = None) -> None:
        if character is not None and character not in CHARACTERS:
            raise ValueError(f"unknown character {character!r} (have {sorted(CHARACTERS)})")
        self.inner = inner
        self.character = character
        self.characters_applied = 0
        self.last_character: str | None = None
        self.cancellations = 0

    # ------------------------------------------------------------------ interface

    def voice_for(self, language: Language) -> str:
        """The default voice *including* the configured character, so a caller that passes
        `voice_for(...)` straight back in — `StreamingSynthesizer` does exactly that — keeps
        the character instead of quietly dropping it."""
        base = self.inner.voice_for(language)
        return f"{self.character}{CHARACTER_SEPARATOR}{base}" if self.character else base

    async def cancel(self) -> None:
        self.cancellations += 1
        await self.inner.cancel()

    async def synthesize(self, request: SynthesisRequest) -> AsyncIterator[SynthesisChunk]:
        spec = parse_voice_spec(request.voice)
        character = spec.character or self.character
        inner_request = request.model_copy(update={"voice": spec.voice})
        if character is None:
            async for chunk in self.inner.synthesize(inner_request):
                yield chunk
            return

        # One chain per synthesis call, built here and not shared: two turns sharing a phase
        # accumulator and a ring buffer means the second one starts mid-effect, and a cancel
        # in the middle of the first would leave that state anywhere at all.
        chain = CharacterChain(character)
        self.characters_applied += 1
        self.last_character = character
        async for chunk in self.inner.synthesize(inner_request):
            # The final chunk carries no audio and must stay byte-empty: the sink treats a
            # non-empty final as audio and a receiver counts it as a gap.
            pcm = chain.process(chunk.pcm) if chunk.pcm else chunk.pcm
            yield SynthesisChunk(seq=chunk.seq, pcm=pcm, is_final=chunk.is_final)

    # ------------------------------------------------------------------ delegation

    async def warm_up(
        self, languages: Sequence[Language] = SUPPORTED_LANGUAGES
    ) -> dict[Language, float]:
        """Delegated, duck-typed — `VoicePipeline.warm_up` calls this on whatever engine it
        was handed. The DSP itself needs no warm-up: it builds scipy filter coefficients in
        microseconds and holds no weights."""
        warm = getattr(self.inner, "warm_up", None)
        if warm is None:
            return {}
        return await warm(languages)

    async def load(self, language: Language, voice: str | None = None) -> None:
        loader = getattr(self.inner, "load", None)
        if loader is None:
            return
        await loader(language, parse_voice_spec(voice).voice)


__all__ = ["CharacterTtsEngine"]
