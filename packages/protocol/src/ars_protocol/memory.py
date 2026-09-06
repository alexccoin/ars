"""Memory and behaviour learning.

Two distinct things live here and must not be confused:

  * MemoryRecord — facts and events A.R.S remembers *about the user's world*.
  * Observation / Preference — what A.R.S has learned about *how the user wants it to
    behave*. These are never inferred silently into permanent effect: an observation
    becomes a preference only when the user confirms it, because a wrong preference
    learned in silence is nearly impossible for the user to find and correct.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from .common import Confidence, Language, Model, new_id, now_ms
from .guard import Provenance


class MemoryKind(StrEnum):
    FACT = "fact"
    EVENT = "event"
    PERSON = "person"
    TASK = "task"
    DOCUMENT = "document"


class Sensitivity(StrEnum):
    """Drives retention, redaction before any cloud call, and deletion priority."""

    PUBLIC = "public"
    PERSONAL = "personal"
    SENSITIVE = "sensitive"
    """Health, finance, credentials, intimate life. Never leaves the device. Never sent
    to a cloud model regardless of backend configuration."""


class MemoryRecord(Model):
    id: str = Field(default_factory=lambda: new_id("mem"))
    kind: MemoryKind
    text: str
    language: Language
    sensitivity: Sensitivity = Sensitivity.PERSONAL
    provenance: Provenance
    created_at_ms: int = Field(default_factory=now_ms)
    valid_until_ms: int | None = None
    """Facts expire. "I live in Cluj" is durable; "I'm on the train" is not."""
    superseded_by: str | None = None
    """Memory is corrected by superseding, never by silent overwrite — the old value
    stays auditable so the user can see what it used to believe and why."""


class BehaviourDomain(StrEnum):
    STYLE = "style"                 # length, tone, formality
    LANGUAGE = "language"           # which language to answer in, when
    PROACTIVITY = "proactivity"     # when to speak unprompted
    CONFIRMATION = "confirmation"   # how often to ask before acting
    SCHEDULE = "schedule"           # quiet hours, routines
    ROUTING = "routing"             # local vs cloud model, which skills


class ObservationStatus(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    EXPIRED = "expired"


class Observation(Model):
    """A hypothesis about how the user wants A.R.S to behave, drawn from what they did.

    Requires `evidence_turn_ids` — at least two independent occurrences before A.R.S may
    even ask. Asking about a one-off is how an assistant becomes exhausting.
    """

    id: str = Field(default_factory=lambda: new_id("obs"))
    domain: BehaviourDomain
    statement: str
    """First person, in the user's language: "Îmi răspunzi mai scurt dimineața."
    This is read back verbatim when confirming, so it must be a sentence the user
    would recognise as their own."""
    confidence: Confidence = 0.0
    evidence_turn_ids: tuple[str, ...] = ()
    status: ObservationStatus = ObservationStatus.PROPOSED
    created_at_ms: int = Field(default_factory=now_ms)
    asked_at_ms: int | None = None
    resolved_at_ms: int | None = None

    @property
    def may_ask(self) -> bool:
        return (
            self.status is ObservationStatus.PROPOSED
            and len(self.evidence_turn_ids) >= 2
            and self.confidence >= 0.6
            and self.asked_at_ms is None
        )


class Preference(Model):
    """A confirmed behaviour rule. These are injected into the system prompt as SYSTEM
    trust, so only user-confirmed observations may become one."""

    id: str = Field(default_factory=lambda: new_id("prf"))
    domain: BehaviourDomain
    statement: str
    from_observation_id: str | None = None
    confirmed_at_ms: int = Field(default_factory=now_ms)
    active: bool = True
