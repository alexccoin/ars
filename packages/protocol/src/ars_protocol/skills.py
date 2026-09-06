"""Skill manifests — what the skills-runtime loads and enforces."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from .capability import Capability
from .common import Language, Model
from .tools import ToolSpec


class SkillRuntime(StrEnum):
    PYTHON_INPROC = "python_inproc"
    """Trusted, first-party skills shipped with A.R.S. Same process, no isolation."""
    PYTHON_SUBPROC = "python_subproc"
    """Third-party skills: separate process, no ambient credentials, network and
    filesystem mediated by the runtime."""
    MCP = "mcp"
    """An external MCP server. Treated as third-party regardless of who wrote it."""


class SkillManifest(Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,48}$")
    version: str
    description: str
    runtime: SkillRuntime
    tools: tuple[ToolSpec, ...]
    capabilities: tuple[Capability, ...]
    """Union of every capability its tools may use. The runtime denies anything outside
    this set even if the tool spec asks for it — defence in depth against a bad manifest."""
    languages: tuple[Language, ...] = ()
    """Languages whose tool descriptions and user-facing strings are localised."""
    author: str | None = None
    trusted: bool = False
    """First-party only. Never settable from a downloaded manifest."""
    network_allowlist: tuple[str, ...] = ()
    """Hostnames this skill may reach. Empty means no network at all."""
