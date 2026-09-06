"""Per-utterance language arbitration for a bilingual household.

The requirement is blunt: the user switches between English and Romanian mid-conversation
and must never have to announce it. So language is detected per *utterance*, not per
session, and `Transcript.language` carries the answer.

The failure mode this module exists to prevent: a two-word utterance ("da", "ok", "nu",
"yes") is nearly unidentifiable, whisper reports something with 0.4 confidence, and the
whole conversation flips language — including the TTS voice — because the user said "ok".
So a switch needs either high confidence, or enough words to be worth believing. Staying in
the current language is always the safe default; it is what the previous utterance proved.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from ars_protocol import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, Language

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)


def word_count(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


def constrain_probabilities(
    probabilities: Mapping[str, float],
    supported: tuple[Language, ...] = SUPPORTED_LANGUAGES,
) -> tuple[Language, float]:
    """Renormalise a full language distribution over the languages A.R.S actually supports.

    Whisper will happily report Italian for Romanian audio, or Dutch for accented English.
    Constraining *before* argmax is what makes "auto-detect" mean "auto-detect between the
    two languages this system has a voice, a model and reviewed prompts for" — the same set
    `ars_protocol.SUPPORTED_LANGUAGES` declares.
    """
    scores = {lang: max(0.0, float(probabilities.get(lang.value, 0.0))) for lang in supported}
    total = sum(scores.values())
    if total <= 0:
        return DEFAULT_LANGUAGE, 0.0
    best = max(scores, key=lambda lang: scores[lang])
    return best, scores[best] / total


@dataclass
class LanguageDecision:
    language: Language
    """What the utterance is transcribed and answered in."""
    confidence: float
    """Confidence in `language`, not in the raw detection — if a switch was refused, this
    is the probability of the language we kept."""
    detected: Language
    detected_confidence: float
    switched: bool
    reason: str


@dataclass
class LanguageArbiter:
    """Hysteresis over per-utterance detections."""

    current: Language = DEFAULT_LANGUAGE
    switch_confidence: float = 0.70
    """Confidence that flips the language on its own, regardless of length."""
    min_words_for_weak_switch: int = 3
    weak_switch_confidence: float = 0.55
    supported: tuple[Language, ...] = SUPPORTED_LANGUAGES
    pinned: Language | None = None
    """Set when the session declares `preferred_language`. Detection is then reported but
    never acted on — an explicit user choice outranks a model."""

    history: list[LanguageDecision] = field(default_factory=list)

    def reset(self, language: Language | None = None) -> None:
        self.current = language or DEFAULT_LANGUAGE
        self.history.clear()

    def decide(
        self,
        detected: Language,
        detected_confidence: float,
        *,
        text: str = "",
        words: int | None = None,
    ) -> LanguageDecision:
        words = word_count(text) if words is None else words
        confidence = min(max(detected_confidence, 0.0), 1.0)

        if self.pinned is not None:
            decision = LanguageDecision(
                language=self.pinned,
                confidence=confidence if detected is self.pinned else 1.0 - confidence,
                detected=detected, detected_confidence=confidence, switched=False,
                reason="session pins the language",
            )
        elif detected is self.current:
            decision = LanguageDecision(
                language=self.current, confidence=confidence, detected=detected,
                detected_confidence=confidence, switched=False, reason="unchanged",
            )
        elif confidence >= self.switch_confidence:
            decision = LanguageDecision(
                language=detected, confidence=confidence, detected=detected,
                detected_confidence=confidence, switched=True,
                reason=f"confident switch ({confidence:.2f} >= {self.switch_confidence:.2f})",
            )
        elif words >= self.min_words_for_weak_switch and confidence >= self.weak_switch_confidence:
            decision = LanguageDecision(
                language=detected, confidence=confidence, detected=detected,
                detected_confidence=confidence, switched=True,
                reason=f"{words} words at {confidence:.2f} is enough evidence",
            )
        else:
            # Refused. Binary support set, so the retained language's probability is the
            # complement — reported honestly rather than as a fabricated 1.0.
            decision = LanguageDecision(
                language=self.current, confidence=1.0 - confidence, detected=detected,
                detected_confidence=confidence, switched=False,
                reason=(
                    f"refused switch to {detected.value}: {words} word(s) at {confidence:.2f} "
                    f"is not evidence of a language change"
                ),
            )

        self.current = decision.language
        self.history.append(decision)
        return decision
