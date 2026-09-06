"""The Guard — provenance, taint, and the decision to allow, ask, or refuse.

An assistant that reads your email and scrapes the web is being fed text written by
people who want things from it. A message can say "forward all invoices to X", a README
can say "run this command". If the model treats that text as instruction, the attacker
now has your grants.

A.R.S's answer: every piece of content carries provenance, provenance carries a trust
level, and a turn that has consumed untrusted content is TAINTED. A tainted turn cannot
silently exercise a capability that touches private data or changes the world — it must
come back to the user, in their own language, and say exactly what it wants to do.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from .capability import Capability, Risk
from .common import Model, SessionId, TurnId, new_id, now_ms


class TrustLevel(StrEnum):
    """Ordered. Compare with the module-level helpers, not with `<` on the string."""

    USER = "user"
    """Spoken or typed by the authenticated user. The only source of instructions."""

    SYSTEM = "system"
    """A.R.S's own prompts, policies and code. Trusted because we shipped it."""

    USER_DATA = "user_data"
    """The user's own stored files, notes, memory. Trusted as *data*, never as instruction —
    the user may have saved an email that contains an attack."""

    EXTERNAL = "external"
    """Anything from outside: web pages, scraped repos, email bodies, tool output from a
    third party. Data only. Always taints the turn."""


_TRUST_ORDER: dict[TrustLevel, int] = {
    TrustLevel.USER: 3, TrustLevel.SYSTEM: 3, TrustLevel.USER_DATA: 2, TrustLevel.EXTERNAL: 0,
}


def taints(level: TrustLevel) -> bool:
    """EXTERNAL content taints a turn. USER_DATA does not on its own, but content
    extracted *from* user data that originated externally is tagged EXTERNAL at ingest."""
    return level is TrustLevel.EXTERNAL


class SourceKind(StrEnum):
    MICROPHONE = "microphone"
    KEYBOARD = "keyboard"
    SYSTEM_PROMPT = "system_prompt"
    MEMORY = "memory"
    LOCAL_FILE = "local_file"
    EMAIL = "email"
    WEB_PAGE = "web_page"
    WEB_SEARCH_RESULT = "web_search_result"
    GITHUB = "github"
    SKILL_OUTPUT = "skill_output"


class Provenance(Model):
    """Where a piece of content came from. Attached at the moment of ingest, never later —
    a retrofitted provenance is a guess, and the guard must not act on guesses."""

    source: SourceKind
    trust: TrustLevel
    uri: str | None = None
    """Concrete origin: a URL, a message-id, a file path. Shown to the user when asking."""
    fetched_at_ms: int = Field(default_factory=now_ms)
    label: str | None = None
    """Short human label for consent prompts, e.g. "email from unknown@example.com"."""


class ContentBlock(Model):
    """Text plus its provenance. The compute service assembles context from these, never
    from bare strings — losing provenance on the way into the prompt is how injections win."""

    text: str
    provenance: Provenance

    @property
    def taints_turn(self) -> bool:
        return taints(self.provenance.trust)


class Verdict(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class DenyReason(StrEnum):
    NO_GRANT = "no_grant"
    GRANT_EXPIRED = "grant_expired"
    GRANT_REVOKED = "grant_revoked"
    RESOURCE_OUT_OF_SCOPE = "resource_out_of_scope"
    TAINTED_TURN = "tainted_turn"
    """The turn consumed untrusted content and this capability is too dangerous to
    exercise on its authority. The single most important rule in this file."""
    GUARD_DISABLED_CAPABILITY = "guard_disabled_capability"
    RATE_LIMITED = "rate_limited"
    POLICY = "policy"


class GuardQuery(Model):
    """A proposed tool call, submitted for judgement BEFORE any side effect occurs."""

    session_id: SessionId
    turn_id: TurnId
    capability: Capability
    resource: str | None = None
    """The concrete thing being touched: a mailbox query, a URL, a file path, a repo."""
    summary: str
    """One sentence, in the user's language, describing the action. This is what gets
    read aloud when the verdict is ASK, so it must be true and specific."""
    tainted: bool = False
    """Set by the compute service from the provenance of everything in the turn's context."""
    taint_sources: tuple[Provenance, ...] = ()


class GuardDecision(Model):
    """The guard's answer. `ASK` is a first-class outcome, not a failure — an assistant
    that never asks is either useless or dangerous."""

    verdict: Verdict
    capability: Capability
    reason: DenyReason | None = None
    explanation: str
    """Plain language, shown and spoken. Never contains model-generated text from a
    tainted source — it is composed by the guard from its own templates."""
    matched_grant_id: str | None = None
    requires_confirmation: bool = False
    decided_at_ms: int = Field(default_factory=now_ms)


class AuditRecord(Model):
    """Append-only record of every guard decision. This log is the only way to answer
    "what did it do with my email last Tuesday", so it is written before the action,
    not after, and it is never rewritten."""

    id: str = Field(default_factory=lambda: new_id("aud"))
    at_ms: int = Field(default_factory=now_ms)
    session_id: SessionId
    turn_id: TurnId
    capability: Capability
    resource: str | None
    verdict: Verdict
    reason: DenyReason | None
    risk: Risk
    tainted: bool
    grant_id: str | None
    user_confirmed: bool | None = None
    outcome: str | None = None
    """Filled in after execution: "ok", or the error class. Never the payload itself —
    audit logs must not become a second copy of the user's private data."""
