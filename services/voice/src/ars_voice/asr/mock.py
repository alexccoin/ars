"""Mock ASR — streams partials from a script, then exactly one final.

Complete enough to run the pipeline end to end with no weights: it consumes the audio at
the real frame rate, so timings measured through it are real timings of everything except
the decode itself, and it goes through the same `LanguageArbiter` as faster-whisper, so the
EN/RO switching rules are exercised by the unit tests rather than only by the model.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field

from ars_core import AsrEngine
from ars_protocol import (
    SUPPORTED_LANGUAGES,
    AudioFrame,
    Language,
    Transcript,
    TranscriptSegment,
)

from ..audio.frames import frame_duration_ms
from .language import LanguageArbiter, word_count


@dataclass
class ScriptedUtterance:
    """What the mock should 'hear' for one call to `transcribe`."""

    text: str
    language: Language = Language.EN
    language_confidence: float = 0.95
    """The *raw detection* confidence. The arbiter decides what happens with it, exactly
    as it would with a number from whisper."""


class MockAsrEngine(AsrEngine):
    """Deterministic, frame-driven, bilingual."""

    name = "mock"

    def __init__(
        self,
        script: Iterable[ScriptedUtterance | str] | None = None,
        *,
        partial_interval_ms: float = 240.0,
        final_decode_ms: float = 0.0,
        arbiter: LanguageArbiter | None = None,
        loop_script: bool = False,
    ) -> None:
        self._script: list[ScriptedUtterance] = [
            ScriptedUtterance(text=item) if isinstance(item, str) else item
            for item in (script or ())
        ]
        self._index = 0
        self.partial_interval_ms = partial_interval_ms
        self.final_decode_ms = final_decode_ms
        self.arbiter = arbiter or LanguageArbiter()
        self.loop_script = loop_script
        self.calls = 0
        self.frames_seen = 0
        self.last_audio_ms = 0.0

    @property
    def supported_languages(self) -> tuple[Language, ...]:
        return SUPPORTED_LANGUAGES

    def queue(self, *utterances: ScriptedUtterance | str) -> None:
        for item in utterances:
            self._script.append(
                ScriptedUtterance(text=item) if isinstance(item, str) else item
            )

    def _next_utterance(self) -> ScriptedUtterance:
        if not self._script:
            return ScriptedUtterance(
                text="", language=self.arbiter.current, language_confidence=0.0
            )
        if self._index >= len(self._script):
            if self.loop_script:
                self._index = 0
            else:
                return ScriptedUtterance(
                    text="", language=self.arbiter.current, language_confidence=0.0
                )
        utterance = self._script[self._index]
        self._index += 1
        return utterance

    async def transcribe(
        self,
        frames: AsyncIterator[AudioFrame],
        *,
        language: Language | None = None,
    ) -> AsyncIterator[Transcript]:
        self.calls += 1
        utterance = self._next_utterance()
        words = utterance.text.split()
        audio_ms = 0.0
        next_partial_at = self.partial_interval_ms
        revealed = 0

        async for frame in frames:
            self.frames_seen += 1
            audio_ms += frame_duration_ms(frame)
            if words and audio_ms >= next_partial_at and revealed < len(words):
                revealed += 1
                next_partial_at += self.partial_interval_ms
                yield Transcript(
                    text=" ".join(words[:revealed]),
                    language=language or utterance.language,
                    language_confidence=utterance.language_confidence,
                    is_final=False,
                    audio_duration_ms=int(audio_ms),
                )

        self.last_audio_ms = audio_ms
        if self.final_decode_ms:
            await asyncio.sleep(self.final_decode_ms / 1000.0)

        # Exactly one final, always — even for an empty utterance. Downstream state machines
        # wait for `is_final`; a silent turn that never produces one deadlocks the session.
        if language is not None:
            resolved, confidence = language, 1.0
        else:
            decision = self.arbiter.decide(
                utterance.language, utterance.language_confidence, text=utterance.text
            )
            resolved, confidence = decision.language, decision.confidence

        yield Transcript(
            text=utterance.text,
            language=resolved,
            language_confidence=confidence,
            is_final=True,
            segments=_segments_for(utterance.text, audio_ms),
            audio_duration_ms=int(audio_ms),
        )


def _segments_for(text: str, audio_ms: float) -> tuple[TranscriptSegment, ...]:
    if not text.strip():
        return ()
    words = text.split()
    if not words:
        return ()
    per_word = audio_ms / len(words)
    segments: list[TranscriptSegment] = []
    for i, word in enumerate(words):
        segments.append(
            TranscriptSegment(
                text=word,
                start_ms=int(i * per_word),
                end_ms=int((i + 1) * per_word),
                confidence=0.99,
            )
        )
    return tuple(segments)


@dataclass
class BilingualScript:
    """Convenience for tests and demos: the same exchange in both languages.

    Every ASR change is evaluated on EN and RO. A fixture set that only has English is a
    fixture set that will let a Romanian regression through.
    """

    en: Sequence[str] = field(default_factory=lambda: ["send an email to Andrei about the invoice"])
    ro: Sequence[str] = field(
        default_factory=lambda: ["trimite un email lui Andrei despre factură"]
    )

    def utterances(self) -> list[ScriptedUtterance]:
        out: list[ScriptedUtterance] = []
        for text in self.en:
            out.append(ScriptedUtterance(text=text, language=Language.EN,
                                         language_confidence=_plausible_confidence(text)))
        for text in self.ro:
            out.append(ScriptedUtterance(text=text, language=Language.RO,
                                         language_confidence=_plausible_confidence(text)))
        return out


def _plausible_confidence(text: str) -> float:
    """Short utterances are genuinely harder to identify. The mock reflects that instead of
    handing every utterance a flattering 0.99, which would hide the arbiter's whole reason
    for existing."""
    words = word_count(text)
    if words <= 1:
        return 0.45
    if words == 2:
        return 0.58
    if words <= 4:
        return 0.78
    return 0.94
