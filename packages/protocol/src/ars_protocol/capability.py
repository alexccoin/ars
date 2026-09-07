"""Capabilities and grants — what A.R.S is allowed to touch, and on whose authority.

A.R.S has a microphone, a memory of the user's life, and credentials for their accounts.
Nothing in this system reaches private data without a grant that the user created
deliberately. This module defines what such a grant looks like.
"""

from __future__ import annotations

import fnmatch
from enum import StrEnum

from pydantic import Field, field_validator

from .common import GrantId, Model, new_id, now_ms


class Risk(StrEnum):
    """How bad it is if this capability is exercised wrongly.

    Drives default confirmation policy: LOW may run silently, HIGH always asks,
    CRITICAL asks and is refused outright inside a tainted turn (see guard.py).
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Capability(StrEnum):
    """The complete set of things A.R.S can be granted. Namespaced `domain.verb`.

    This enum is closed on purpose. A skill cannot invent a capability at runtime —
    if something new is needed it lands here, in a reviewed diff, with a risk level.
    """

    # Reading the world — low risk, no private data
    WEB_SEARCH = "web.search"
    WEB_FETCH = "web.fetch"
    GITHUB_READ_PUBLIC = "github.read_public"

    # The user's own accounts — private data
    EMAIL_READ = "email.read"
    EMAIL_SEARCH = "email.search"
    CALENDAR_READ = "calendar.read"
    CONTACTS_READ = "contacts.read"
    GITHUB_READ_PRIVATE = "github.read_private"
    FILES_READ = "files.read"

    # Acting on the user's behalf — irreversible, visible to other people
    EMAIL_SEND = "email.send"
    CALENDAR_WRITE = "calendar.write"
    FILES_WRITE = "files.write"
    GITHUB_WRITE = "github.write"
    APP_CONTROL = "app.control"

    # Machine-level power
    SHELL_EXEC = "shell.exec"
    NETWORK_EGRESS = "network.egress"

    # Clinical knowledge — a curated corpus, not the user's own record
    MEDICAL_READ = "medical.read"

    # The assistant's own state
    MEMORY_READ = "memory.read"
    MEMORY_WRITE = "memory.write"

    @property
    def risk(self) -> Risk:
        return _RISK[self]

    @property
    def touches_private_data(self) -> bool:
        """True if exercising this can expose data the user would not publish."""
        return self in _PRIVATE

    @property
    def exfiltrates_outward(self) -> bool:
        """True if exercising this sends text of our choosing to somewhere outside.

        Easy to miss, because a web search is otherwise the most harmless thing A.R.S
        does. But "search the web for <the user's private notes>" is a working data
        exfiltration channel, and in a turn that has already read attacker-controlled
        text, an attacker gets to choose the query. The guard cannot see content, so
        the control is to surface the literal query to the user before it leaves.
        """
        return self in _EXFILTRATES

    @property
    def is_effectful(self) -> bool:
        """True if exercising this changes the world outside A.R.S. Never auto-retry these."""
        return self in _EFFECTFUL


_RISK: dict[Capability, Risk] = {
    Capability.WEB_SEARCH: Risk.LOW,
    Capability.WEB_FETCH: Risk.LOW,
    Capability.GITHUB_READ_PUBLIC: Risk.LOW,
    Capability.MEMORY_READ: Risk.LOW,
    Capability.MEMORY_WRITE: Risk.MEDIUM,
    Capability.EMAIL_READ: Risk.HIGH,
    Capability.EMAIL_SEARCH: Risk.HIGH,
    Capability.CALENDAR_READ: Risk.MEDIUM,
    Capability.CONTACTS_READ: Risk.HIGH,
    Capability.GITHUB_READ_PRIVATE: Risk.HIGH,
    Capability.FILES_READ: Risk.HIGH,
    Capability.EMAIL_SEND: Risk.CRITICAL,
    Capability.CALENDAR_WRITE: Risk.HIGH,
    Capability.FILES_WRITE: Risk.HIGH,
    Capability.GITHUB_WRITE: Risk.CRITICAL,
    Capability.APP_CONTROL: Risk.HIGH,
    Capability.SHELL_EXEC: Risk.CRITICAL,
    Capability.NETWORK_EGRESS: Risk.MEDIUM,
    # HIGH, not MEDIUM, and the reason is not the data — a clinical protocol is written to
    # be read. It is what a wrong answer costs. Every other HIGH capability here is graded
    # on what it exposes; this one is graded on what someone might do because of it.
    Capability.MEDICAL_READ: Risk.HIGH,
}

_PRIVATE: frozenset[Capability] = frozenset({
    Capability.EMAIL_READ, Capability.EMAIL_SEARCH, Capability.CALENDAR_READ,
    Capability.CONTACTS_READ, Capability.GITHUB_READ_PRIVATE, Capability.FILES_READ,
    Capability.MEMORY_READ, Capability.APP_CONTROL,
    # Not because the corpus is private, but because the QUESTION is: what someone asks a
    # medical assistant is among the most sensitive things they will ever type, and a
    # capability that is not private never asks before it is used.
    Capability.MEDICAL_READ,
})

_EFFECTFUL: frozenset[Capability] = frozenset({
    Capability.EMAIL_SEND, Capability.CALENDAR_WRITE, Capability.FILES_WRITE,
    Capability.GITHUB_WRITE, Capability.SHELL_EXEC, Capability.APP_CONTROL,
    Capability.MEMORY_WRITE,
})


_EXFILTRATES: frozenset[Capability] = frozenset({
    Capability.WEB_SEARCH, Capability.WEB_FETCH, Capability.NETWORK_EGRESS,
    Capability.EMAIL_SEND, Capability.GITHUB_WRITE,
})


class ConfirmPolicy(StrEnum):
    """How often the user wants to be asked before this grant is used."""

    NEVER = "never"          # silent — only ever appropriate for LOW risk
    ONCE_PER_SESSION = "once_per_session"
    EVERY_USE = "every_use"


class GrantSource(StrEnum):
    """How the grant was created. Voice grants are convenient and therefore suspect:
    they can be created by anyone the microphone can hear."""

    VOICE = "voice"
    UI = "ui"
    CONFIG = "config"


class CapabilityGrant(Model):
    """A user's standing permission for A.R.S to use one capability.

    Grants are narrow by construction: a capability alone is not enough, it must also
    match `resource_patterns`. `email.read` scoped to `from:bank.ro` is a different
    grant from unrestricted mailbox access, and the difference is enforced, not documented.
    """

    id: GrantId = Field(default_factory=lambda: new_id("grn"))
    capability: Capability
    resource_patterns: tuple[str, ...] = ("*",)
    """glob patterns matched against the tool call's `resource`. `*` means unrestricted —
    require a deliberate choice to use it for anything above LOW risk."""

    confirm: ConfirmPolicy = ConfirmPolicy.EVERY_USE
    granted_at_ms: int = Field(default_factory=now_ms)
    expires_at_ms: int | None = None
    """None means no expiry. The auth service still nudges the user to review HIGH+ grants."""

    source: GrantSource = GrantSource.UI
    note: str | None = None
    """The user's own words for why they granted this — shown back when asking to renew."""

    revoked_at_ms: int | None = None

    @field_validator("resource_patterns")
    @classmethod
    def _no_empty_patterns(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError(
                "a grant with no resource patterns grants nothing; "
                "use ('*',) to mean all"
            )
        return v

    def is_active(self, at_ms: int | None = None) -> bool:
        at = at_ms if at_ms is not None else now_ms()
        if self.revoked_at_ms is not None and self.revoked_at_ms <= at:
            return False
        return not (self.expires_at_ms is not None and self.expires_at_ms <= at)

    def covers(self, capability: Capability, resource: str | None,
               at_ms: int | None = None) -> bool:
        """Exact capability match plus glob match on the resource. No implicit hierarchy:
        `email.read` never implies `email.send`, and nothing implies SHELL_EXEC."""
        if capability is not self.capability or not self.is_active(at_ms):
            return False
        if resource is None:
            return "*" in self.resource_patterns
        return any(fnmatch.fnmatchcase(resource, p) for p in self.resource_patterns)


class GrantRequest(Model):
    """A.R.S asking for access it does not have. Shown to the user as a consent prompt
    in their own language, with `reason` quoted verbatim — never paraphrased by the model
    at display time, or the prompt becomes an injection surface."""

    capability: Capability
    resource_patterns: tuple[str, ...] = ("*",)
    reason: str
    """Plain-language justification, spoken and shown. Must name the concrete task."""
    suggested_confirm: ConfirmPolicy = ConfirmPolicy.EVERY_USE
    suggested_duration_ms: int | None = None
