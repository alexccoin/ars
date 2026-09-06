"""Base class for every A.R.S skill."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ars_protocol import Provenance, SkillManifest, ToolResult, ToolStatus

from .context import SkillContext


class Skill(ABC):
    """A capability provider. Subclasses declare a manifest and implement `call`."""

    @property
    @abstractmethod
    def manifest(self) -> SkillManifest: ...

    @abstractmethod
    async def call(self, tool: str, args: dict[str, Any], ctx: SkillContext) -> list[tuple[str,
    Provenance]]:
        """Return content blocks WITH provenance. Returning a bare string is impossible
        by construction — that is the point of this signature."""

    def ok(self, call_id: str, tool: str, blocks: list[tuple[str, Provenance]],
           duration_ms: int, truncated: bool = False) -> ToolResult:
        return ToolResult(call_id=call_id, tool=tool, status=ToolStatus.OK,
                          content=tuple(blocks), duration_ms=duration_ms, truncated=truncated)


class SkillError(RuntimeError):
    """Expected, reportable failure (bad query, not found, upstream 4xx).

    Distinct from an unexpected exception: these are summarised back to the user in
    their own language, unexpected ones are logged and reported as an internal error
    without leaking a stack trace into the conversation.
    """
