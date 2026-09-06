"""Endpointing — deciding the user has finished speaking.

Kept deliberately free of audio, models and asyncio: it consumes a boolean per frame plus
(optionally) the running partial transcript, and emits a decision. That makes it testable
against fixtures at thousands of times realtime, which is the only way to tune it honestly.

The bias of every default in here: **cutting the user off is worse than making them wait.**
A 300 ms saving is not worth truncating one sentence in fifty. Three mechanisms enforce it:

1. a minimum-utterance guard — a cough or the tail of the wakeword cannot endpoint a turn;
2. trailing-hesitation handling — an utterance ending in "uhm", "and", "păi", "și" buys
   extra silence, because the user is thinking, not finished;
3. a silence window that is generous by default (700 ms, from `VoiceConfig`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from ars_protocol import FRAME_MS, Language

# The curly apostrophe is deliberate: it is what ASR output actually contains.
_WORD_RE = re.compile(r"[\w'’ăâîșşțţĂÂÎȘŞȚŢ]+", re.UNICODE)  # noqa: RUF001

# Words that are almost never the last word of a finished utterance. Deliberately narrow:
# every false positive here costs `hesitation_grace_ms` on a turn that was already over, and
# "that", "like", "when" are common sentence-final words that used to sit in this list and
# added 400 ms to "stop that" — measured on data/fixtures/endpointing.
DEFAULT_HESITATION_EN: tuple[str, ...] = (
    "uh", "um", "uhm", "er", "erm", "hmm", "and", "but", "so", "or", "because",
    "the", "a", "an", "to", "of", "for", "with", "my", "your",
)
DEFAULT_HESITATION_RO: tuple[str, ...] = (
    "aa", "ăă", "ăăă", "păi", "pai", "deci", "si", "și", "dar", "sau", "că", "ca", "cu",
    "la", "de", "pe", "un", "o", "în", "in", "adică", "adica", "sa", "să", "mai",
)


class EndpointState(StrEnum):
    IDLE = "idle"
    """Waiting for the utterance to begin."""
    SPEECH = "speech"
    TRAILING = "trailing"
    """Speech has stopped; the silence clock is running."""
    ENDPOINTED = "endpointed"
    ABANDONED = "abandoned"
    """The mic opened and nobody spoke. Go back to IDLE rather than hold it open."""


class EndpointReason(StrEnum):
    SILENCE = "silence"
    MAX_DURATION = "max_duration"
    NO_SPEECH = "no_speech"


@dataclass(frozen=True)
class EndpointStep:
    """The result of feeding one frame. Returned every frame so callers can timestamp the
    exact frame where speech ended, which is where the ENDPOINTING budget starts."""

    state: EndpointState
    changed: bool = False
    is_endpoint: bool = False
    reason: EndpointReason | None = None
    utterance_ms: float = 0.0
    speech_ms: float = 0.0
    trailing_silence_ms: float = 0.0
    required_silence_ms: float = 0.0
    hesitation_extended: bool = False

    @property
    def speech_just_ended(self) -> bool:
        return self.changed and self.state is EndpointState.TRAILING


@dataclass
class Endpointer:
    """Silence-based endpointing with a minimum-utterance guard and hesitation handling.

    `silence_ms` comes from `VoiceConfig.endpoint_silence_ms` (default 700). Do not pass a
    literal here from application code — read the config, so one setting governs.
    """

    silence_ms: float = 700.0
    min_utterance_ms: float = 320.0
    max_utterance_ms: float = 30_000.0
    speech_onset_ms: float = 100.0
    resume_onset_ms: float = 60.0
    """Speech needed to *cancel* a running silence timer.

    Without this, one 20 ms noise frame resets a 700 ms countdown, and measured endpoint
    latency on data/fixtures/endpointing swung by 340 ms on a fixture whose room noise
    happened to peak once. A blip is not the user resuming."""
    hesitation_grace_ms: float = 400.0
    no_speech_timeout_ms: float = 6_000.0
    adaptive: bool = False
    confident_silence_ms: float = 420.0
    hesitation_en: tuple[str, ...] = DEFAULT_HESITATION_EN
    hesitation_ro: tuple[str, ...] = DEFAULT_HESITATION_RO

    state: EndpointState = field(default=EndpointState.IDLE, init=False)
    speech_ms: float = field(default=0.0, init=False)
    trailing_silence_ms: float = field(default=0.0, init=False)
    utterance_ms: float = field(default=0.0, init=False)
    idle_ms: float = field(default=0.0, init=False)
    hesitation_extended: bool = field(default=False, init=False)

    _onset_run_ms: float = field(default=0.0, init=False)
    _resume_run_ms: float = field(default=0.0, init=False)
    _partial_text: str = field(default="", init=False)
    _partial_language: Language = field(default=Language.EN, init=False)

    def reset(self) -> None:
        self.state = EndpointState.IDLE
        self.speech_ms = 0.0
        self.trailing_silence_ms = 0.0
        self.utterance_ms = 0.0
        self.idle_ms = 0.0
        self.hesitation_extended = False
        self._onset_run_ms = 0.0
        self._resume_run_ms = 0.0
        self._partial_text = ""
        self._partial_language = Language.EN

    # ------------------------------------------------------------------ input

    def note_partial(self, text: str, language: Language = Language.EN) -> None:
        """Give the endpointer the running ASR partial.

        Optional — endpointing must work with no ASR at all — but when it is available,
        "send an email to…" is visibly unfinished and deserves more patience than
        "send an email to Andrei".
        """
        self._partial_text = text or ""
        self._partial_language = language

    def update(self, is_speech: bool, frame_ms: float = float(FRAME_MS)) -> EndpointStep:
        if self.state in (EndpointState.ENDPOINTED, EndpointState.ABANDONED):
            return self._step(changed=False)

        if self.state is EndpointState.IDLE:
            return self._update_idle(is_speech, frame_ms)

        self.utterance_ms += frame_ms
        if is_speech:
            return self._update_speech(frame_ms)
        return self._update_silence(frame_ms)

    # ------------------------------------------------------------------ internals

    def _update_idle(self, is_speech: bool, frame_ms: float) -> EndpointStep:
        if not is_speech:
            self.idle_ms += frame_ms
            self._onset_run_ms = 0.0
            if self.idle_ms >= self.no_speech_timeout_ms:
                self.state = EndpointState.ABANDONED
                return self._step(changed=True, is_endpoint=True, reason=EndpointReason.NO_SPEECH)
            return self._step(changed=False)

        # Require a run of speech frames before declaring SPEECH_START: a single loud frame
        # is a door, a keyboard or a plosive on the mic, not the user starting a sentence.
        self._onset_run_ms += frame_ms
        if self._onset_run_ms < self.speech_onset_ms:
            return self._step(changed=False)

        self.state = EndpointState.SPEECH
        self.speech_ms = self._onset_run_ms
        self.utterance_ms = self._onset_run_ms
        self.trailing_silence_ms = 0.0
        return self._step(changed=True)

    def _update_speech(self, frame_ms: float) -> EndpointStep:
        self.speech_ms += frame_ms
        if self.state is EndpointState.TRAILING:
            # In trailing silence, speech only counts as 'the user carried on' once it has
            # persisted. A shorter run is noise and leaves the silence clock running.
            self._resume_run_ms += frame_ms
            if self._resume_run_ms < self.resume_onset_ms:
                self.trailing_silence_ms += frame_ms
                return self._step(changed=False)
        changed = self.state is not EndpointState.SPEECH
        self._resume_run_ms = 0.0
        self.state = EndpointState.SPEECH
        self.trailing_silence_ms = 0.0
        if self.utterance_ms >= self.max_utterance_ms:
            self.state = EndpointState.ENDPOINTED
            return self._step(changed=True, is_endpoint=True, reason=EndpointReason.MAX_DURATION)
        return self._step(changed=changed)

    def _update_silence(self, frame_ms: float) -> EndpointStep:
        changed = self.state is not EndpointState.TRAILING
        self.state = EndpointState.TRAILING
        self._resume_run_ms = 0.0
        self.trailing_silence_ms += frame_ms

        if self.utterance_ms >= self.max_utterance_ms:
            self.state = EndpointState.ENDPOINTED
            return self._step(changed=True, is_endpoint=True, reason=EndpointReason.MAX_DURATION)

        # The minimum-utterance guard. Silence after 80 ms of "speech" is not the end of a
        # turn; it is a noise. Fall back to IDLE and keep listening rather than endpoint an
        # empty utterance and hand ASR a cough.
        if self.speech_ms < self.min_utterance_ms:
            if self.trailing_silence_ms >= self.required_silence_ms:
                self.state = EndpointState.IDLE
                self.speech_ms = 0.0
                self.trailing_silence_ms = 0.0
                self.utterance_ms = 0.0
                self._onset_run_ms = 0.0
                self._resume_run_ms = 0.0
                return self._step(changed=True)
            return self._step(changed=changed)

        if self.trailing_silence_ms >= self.required_silence_ms:
            self.state = EndpointState.ENDPOINTED
            return self._step(changed=True, is_endpoint=True, reason=EndpointReason.SILENCE)
        return self._step(changed=changed)

    @property
    def required_silence_ms(self) -> float:
        """How much trailing silence this particular utterance needs.

        Base window, minus an adaptive discount when the utterance looks complete, plus a
        grace period when it trails off mid-thought. The grace is applied last and always
        wins: patience beats latency at every tie.
        """
        base = self.silence_ms
        if self.adaptive and not self._looks_unfinished() and self._looks_substantial():
            base = min(base, self.confident_silence_ms)
        if self._looks_unfinished():
            base += self.hesitation_grace_ms
        return base

    def _tokens(self) -> list[str]:
        return [t.lower() for t in _WORD_RE.findall(self._partial_text)]

    def _looks_substantial(self) -> bool:
        """Enough evidence that the utterance is a complete thought to risk a shorter wait."""
        return self.speech_ms >= 3 * self.min_utterance_ms and len(self._tokens()) >= 3

    def _looks_unfinished(self) -> bool:
        """Trailing hesitation: the last token is a filler or a dangling function word, or
        the transcript ends on a comma. Both languages, because a household that switches
        mid-sentence gets cut off in whichever one is not handled."""
        text = self._partial_text.rstrip()
        if text.endswith((",", "…", "-", "—")):
            return True
        tokens = self._tokens()
        if not tokens:
            return False
        vocabulary = (
            self.hesitation_ro if self._partial_language is Language.RO else self.hesitation_en
        )
        # Check both vocabularies for the RO case: Romanian speakers code-switch "and"/"si"
        # freely, and a missed filler costs a truncation.
        both = set(vocabulary)
        if self._partial_language is Language.RO:
            both |= set(self.hesitation_ro)
        return tokens[-1] in both

    def _step(
        self,
        *,
        changed: bool,
        is_endpoint: bool = False,
        reason: EndpointReason | None = None,
    ) -> EndpointStep:
        self.hesitation_extended = self._looks_unfinished()
        return EndpointStep(
            state=self.state,
            changed=changed,
            is_endpoint=is_endpoint,
            reason=reason,
            utterance_ms=self.utterance_ms,
            speech_ms=self.speech_ms,
            trailing_silence_ms=self.trailing_silence_ms,
            required_silence_ms=self.required_silence_ms,
            hesitation_extended=self.hesitation_extended,
        )


def endpointer_from_config(config) -> Endpointer:
    """Build an Endpointer from `VoicePipelineConfig`. The silence window comes from
    `core.endpoint_silence_ms` — one setting, one source."""
    ep = config.endpointing
    return Endpointer(
        silence_ms=float(config.core.endpoint_silence_ms),
        min_utterance_ms=float(ep.min_utterance_ms),
        max_utterance_ms=float(ep.max_utterance_ms),
        speech_onset_ms=float(ep.speech_onset_ms),
        hesitation_grace_ms=float(ep.hesitation_grace_ms),
        resume_onset_ms=float(ep.resume_onset_ms),
        no_speech_timeout_ms=float(ep.no_speech_timeout_ms),
        adaptive=ep.adaptive,
        confident_silence_ms=float(ep.confident_silence_ms),
        hesitation_en=ep.trailing_hesitation_tokens_en,
        hesitation_ro=ep.trailing_hesitation_tokens_ro,
    )
