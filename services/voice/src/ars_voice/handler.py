"""The seam between voice and reasoning.

`services/voice` must not import `services/compute`: the gateway wires them together. This
is the smallest interface that lets the voice pipeline own barge-in properly — it needs to
be able to *cancel* the in-flight reasoning call, not just stop reading from it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import ClassVar

from ars_protocol import Language, Transcript, Turn


class TurnHandler(ABC):
    """Produces the assistant's reply for one user utterance, as a text stream."""

    @abstractmethod
    def respond(self, transcript: Transcript, turn: Turn) -> AsyncIterator[str]:
        """Stream reply text deltas. The pipeline synthesises from the first sentence
        boundary, so yield early and often rather than in one block."""

    @abstractmethod
    async def cancel(self) -> None:
        """Barge-in reached us. Abort the in-flight compute request.

        Implementations must actually cancel the request — an LLM call left running on a
        cancelled turn costs the user their battery and, on a cloud backend, their money and
        their privacy budget.
        """

    def reply_language(self, transcript: Transcript) -> Language:
        """Answer in the language the user spoke, unless overridden."""
        return transcript.language

    def reply_voice(self, transcript: Transcript) -> str | None:
        """Which voice to answer in, or None for the language's configured default.

        The seam exists because WHO is being spoken to is known on the reasoning side and
        nowhere else. `services/voice` cannot tell that a question is about somebody's
        child, and should not learn how — that is product knowledge, and it changes. This
        is the same shape as `reply_language`: a decision the handler owns, applied by the
        pipeline.
        """
        return None


class EchoTurnHandler(TurnHandler):
    """Repeats what it heard, in the language it was heard in.

    Used by tests and by the standalone voice demo. Deliberately bilingual so a run without
    a compute service still exercises both EN and RO end to end.
    """

    PREFIX: ClassVar[dict[Language, str]] = {Language.EN: "You said", Language.RO: "Ai spus"}
    EMPTY: ClassVar[dict[Language, str]] = {
        Language.EN: "I did not catch that.",
        Language.RO: "Nu am înțeles.",
    }

    def __init__(self, *, delta_words: int = 3, first_token_delay_ms: float = 0.0) -> None:
        self.delta_words = max(1, delta_words)
        self.first_token_delay_ms = first_token_delay_ms
        self.cancellations = 0
        self.calls = 0
        self.cancelled = False

    async def respond(self, transcript: Transcript, turn: Turn) -> AsyncIterator[str]:
        import asyncio

        self.calls += 1
        self.cancelled = False
        language = self.reply_language(transcript)
        if not transcript.text.strip():
            yield self.EMPTY[language]
            return
        if self.first_token_delay_ms:
            await asyncio.sleep(self.first_token_delay_ms / 1000.0)
        text = f"{self.PREFIX[language]}: {transcript.text}."
        words = text.split()
        for start in range(0, len(words), self.delta_words):
            yield " ".join(words[start : start + self.delta_words]) + " "

    async def cancel(self) -> None:
        self.cancellations += 1
        self.cancelled = True
