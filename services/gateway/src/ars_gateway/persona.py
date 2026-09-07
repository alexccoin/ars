"""Who A.R.S is being, this turn.

Alex asked for one specific thing: when the subject is his young son, A.R.S should sound
like a small rescue-dog companion and look like one too. That is not decoration — a
seven-year-old and an adult want different things from the same assistant, and switching
both the voice and the face is how a machine says "I am talking to you now" without
anybody explaining it.

Two decisions worth stating.

**The name is configuration, never source.** A child's name hardcoded into a repository is
a child's name published. `ARS_COMPANION_NAME` is empty by default, and with it empty this
whole module is inert.

**Nothing here is a clone of anybody.** The voice is `pup_*` from the catalogue: a real
licensed voice shifted upward so it sounds like a physically smaller speaker, with the
gentlest effect chain in the register over it. It is not trained on, scraped from, or
imitating any performance. Building it that way was a choice, and it is the reason this
can ship.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from ars_protocol import Language


class Persona(StrEnum):
    DEFAULT = "default"
    COMPANION = "companion"
    """For the child. Different voice, different face, simpler answers."""


@dataclass(frozen=True)
class PersonaChoice:
    persona: Persona
    voice: str | None
    """A `SynthesisRequest.voice` spec, or None to leave the configured voice alone."""

    reason: str
    """Why this persona was chosen. Shown in the interface rather than hidden, because a
    machine that changes its voice without saying why is unsettling rather than friendly."""


_COMPANION_VOICE = {
    Language.EN: "pup/pup_en",
    Language.RO: "pup/pup_ro",
    Language.DE: "pup/pup_de",
}


class PersonaPicker:
    """Chooses a persona from the text of a turn.

    Deliberately a whole-word match on one configured name and nothing cleverer. A model
    could be asked "is this about the child?" and would be right more often and wrong
    unpredictably; the failure mode of a regex is that it does not fire, which is a turn
    in the ordinary voice. The failure mode of a classifier is that it fires on a stranger's
    message, and this persona exists precisely to signal WHO is being spoken to.
    """

    def __init__(self, name: str | None) -> None:
        self.name = (name or "").strip()
        self._pattern = (
            re.compile(rf"\b{re.escape(self.name)}\b", re.IGNORECASE) if self.name else None
        )

    @property
    def enabled(self) -> bool:
        return self._pattern is not None

    def choose(self, text: str, language: Language) -> PersonaChoice:
        if self._pattern is not None and self._pattern.search(text or ""):
            return PersonaChoice(
                persona=Persona.COMPANION,
                voice=_COMPANION_VOICE.get(language),
                reason=f"{self.name} is the subject",
            )
        return PersonaChoice(persona=Persona.DEFAULT, voice=None, reason="")


__all__ = ["Persona", "PersonaChoice", "PersonaPicker"]
