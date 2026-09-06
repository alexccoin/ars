"""Sentence segmentation for streaming synthesis, EN + RO.

Time-to-first-audio is the whole reason this file exists. The reply arrives from the LLM as
a token stream; waiting for it to finish before synthesising would put first audio hundreds
of milliseconds — often seconds — past the 120 ms budget. So we synthesise the first
sentence the moment it is complete, and keep up with the rest.

The second reason: a sentence is the right unit for prosody. Handing Piper half a clause
produces audibly wrong intonation, so the split points are sentence ends, not token counts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TERMINATORS = ".!?…"
_CLOSERS = "\"'”’)]»"  # noqa: RUF001 - typographic quotes are real characters in real text

# Abbreviations whose full stop does not end a sentence. Both languages, because "dl." and
# "nr." mid-sentence would otherwise make Piper stop dead in the middle of a Romanian reply.
_ABBREVIATIONS = frozenset(
    {
        "mr", "mrs", "ms", "dr", "prof", "st", "vs", "etc", "eg", "ie", "approx", "inc", "no",
        "dl", "dna", "dra", "nr", "str", "bd", "sos", "ex", "ș", "s",
    }
)

_SENTENCE_END = re.compile(rf"[{re.escape(_TERMINATORS)}]+[{re.escape(_CLOSERS)}]*\s")
_WORD_TAIL = re.compile(r"([^\W\d_]+)\.$", re.UNICODE)


def _is_abbreviation(candidate: str) -> bool:
    match = _WORD_TAIL.search(candidate.rstrip())
    if not match:
        return False
    return match.group(1).lower() in _ABBREVIATIONS


def _ends_in_number(candidate: str) -> bool:
    """'3.' inside '3.14' or a list item is not a sentence end."""
    return bool(re.search(r"\d\.$", candidate.rstrip()))


@dataclass
class SentenceStreamer:
    """Incremental sentence splitter over a text stream.

    `first_max_chars` is a safety valve: a reply whose first sentence is a 400-character
    paragraph must not hold first audio hostage. Past that length we cut at the last comma
    or space, which is a slightly worse prosodic break and a much better latency.
    """

    first_max_chars: int = 140
    later_max_chars: int = 400
    min_chars: int = 8
    buffer: str = field(default="", init=False)
    emitted: int = field(default=0, init=False)

    def reset(self) -> None:
        self.buffer = ""
        self.emitted = 0

    @property
    def _max_chars(self) -> int:
        return self.first_max_chars if self.emitted == 0 else self.later_max_chars

    def push(self, text: str) -> list[str]:
        """Feed a delta; get back the sentences that are now complete."""
        self.buffer += text
        return self._drain()

    def flush(self) -> list[str]:
        """End of stream: emit whatever is left, complete or not."""
        out = self._drain()
        tail = self.buffer.strip()
        self.buffer = ""
        if tail:
            self.emitted += 1
            out.append(tail)
        return out

    def _drain(self) -> list[str]:
        out: list[str] = []
        while True:
            sentence = self._take_one()
            if sentence is None:
                return out
            out.append(sentence)

    def _take_one(self) -> str | None:
        for match in _SENTENCE_END.finditer(self.buffer):
            end = match.end()
            candidate = self.buffer[:end]
            stripped = candidate.rstrip()
            if len(stripped) < self.min_chars:
                continue
            if _is_abbreviation(stripped) or _ends_in_number(stripped):
                continue
            self.buffer = self.buffer[end:]
            self.emitted += 1
            return stripped

        # Newline is a hard break: a list or a paragraph boundary is a fine place to breathe.
        newline = self.buffer.find("\n")
        if newline >= 0 and len(self.buffer[:newline].strip()) >= self.min_chars:
            sentence = self.buffer[:newline].strip()
            self.buffer = self.buffer[newline + 1 :]
            self.emitted += 1
            return sentence

        if len(self.buffer) > self._max_chars:
            cut = self._soft_cut(self.buffer[: self._max_chars])
            if cut >= self.min_chars:
                sentence = self.buffer[:cut].strip()
                self.buffer = self.buffer[cut:]
                self.emitted += 1
                return sentence
        return None

    @staticmethod
    def _soft_cut(window: str) -> int:
        for separator in (", ", "; ", ": ", " — ", " - ", " "):
            index = window.rfind(separator)
            if index > 0:
                return index + len(separator)
        return len(window)


def split_sentences(text: str, *, first_max_chars: int = 140) -> list[str]:
    """One-shot split, for non-streaming callers and tests."""
    streamer = SentenceStreamer(first_max_chars=first_max_chars)
    out = streamer.push(text)
    out.extend(streamer.flush())
    return [s for s in out if s.strip()]
