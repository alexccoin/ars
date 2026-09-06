"""Tool (skill) invocation contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field

from .capability import Capability
from .common import Model, new_id, now_ms
from .guard import Provenance


class ToolParam(Model):
    name: str
    type: str
    description: str
    required: bool = True
    enum: tuple[str, ...] | None = None


class ToolSpec(Model):
    """What the reasoning layer is told a skill can do.

    `capabilities` is not documentation — the runtime refuses to execute a tool that
    reaches for a capability it did not declare here, so this list is the sandbox.
    """

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,48}$")
    description: str
    params: tuple[ToolParam, ...] = ()
    capabilities: tuple[Capability, ...] = ()
    returns_external_content: bool = False
    """True for anything that fetches text written by someone other than the user
    (web, email, GitHub). Output from these is tagged EXTERNAL and taints the turn."""


class ToolCall(Model):
    id: str = Field(default_factory=lambda: new_id("tc"))
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    requested_at_ms: int = Field(default_factory=now_ms)


class ToolStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    DENIED = "denied"
    NEEDS_CONSENT = "needs_consent"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class ToolResult(Model):
    """Result of a tool call.

    `content` blocks carry their own provenance so the compute service can keep untrusted
    text quarantined all the way into the prompt. A tool that returns a bare string is
    not permitted by this protocol — that is the whole point.
    """

    call_id: str
    tool: str
    status: ToolStatus
    content: tuple[tuple[str, Provenance], ...] = ()
    error: str | None = None
    duration_ms: int = Field(default=0, ge=0)
    truncated: bool = False
