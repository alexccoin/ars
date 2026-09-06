"""Remove the wake phrase from the start of an utterance.

Found by running the real pipeline: openWakeWord fires at the *end* of the keyword, and the
pre-roll ring buffer — which exists so the first syllable of the command is not clipped —
therefore also hands ASR the last few hundred milliseconds of the keyword itself. Measured
with `hey_jarvis` and a 500 ms pre-roll, the final transcript came back as

    "Jarvis, good morning, I found three new messages from the bank..."

The user did not say "Jarvis" to the assistant; they said it *at* the assistant. Passing it
on puts a stray vocative into the reasoning layer's context on every single turn, and into
memory.

Shortening the pre-roll is the wrong fix: it trades a cosmetic problem for a truncated first
word, which is the failure the pre-roll exists to prevent. So the audio keeps the keyword and
the text drops it.

Only a *leading* match is removed, and only on a turn that began with a wake event, so
"remind me to call Jarvis" mid-sentence is untouched.
"""

from __future__ import annotations

import re

_LEADING_PUNCTUATION = " ,.;:!?-—…\"'“”’"  # noqa: RUF001 - real transcripts contain these


def spoken_forms(keyword: str) -> tuple[str, ...]:
    """Ways a keyword id can appear in a transcript, longest first.

    `hey_jarvis` is spoken "hey jarvis" and transcribed either that way or as just "Jarvis"
    when the pre-roll only caught the tail. Both have to go, longest first so the two-word
    form is not left with a dangling "hey".
    """
    words = [part for part in re.split(r"[_\-\s]+", keyword.strip().lower()) if part]
    if not words:
        return ()
    forms = {" ".join(words)}
    if len(words) > 1:
        # The name without its carrier word ("hey", "ok", "hi").
        forms.add(" ".join(words[1:]))
        forms.add(words[-1])
    return tuple(sorted(forms, key=len, reverse=True))


def strip_wakeword_prefix(text: str, keyword: str) -> str:
    """Drop a leading wake phrase. Returns `text` unchanged if there is not one."""
    if not text or not keyword:
        return text
    stripped = text.lstrip()
    for form in spoken_forms(keyword):
        pattern = re.compile(
            r"^" + r"[\s,]*".join(re.escape(word) for word in form.split()) + r"\b",
            re.IGNORECASE,
        )
        match = pattern.match(stripped)
        if not match:
            continue
        remainder = stripped[match.end() :].lstrip(_LEADING_PUNCTUATION)
        if not remainder:
            # The whole utterance was the wake phrase. Keep it: "hey jarvis" on its own is a
            # real turn ("yes?"), and returning "" would look like a failed transcription.
            return text
        return remainder[0].upper() + remainder[1:] if remainder[0].islower() else remainder
    return text
