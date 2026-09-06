"""The skill runtime — the last line of defence before A.R.S touches the real world.

The guard has already ruled on this call. This layer checks again anyway. That is not
redundancy for its own sake: the guard reasons about *policy*, this reasons about
*declarations*, and a skill that reaches for a capability it never declared is a
compromised or buggy skill regardless of what the user granted.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from ars_core import SkillRuntime as SkillRuntimeInterface
from ars_protocol import (
    Capability,
    GuardQuery,
    ToolCall,
    ToolResult,
    ToolSpec,
    ToolStatus,
    Verdict,
)

from .base import Skill, SkillError
from .context import MediatedHttp, NetworkDenied, SkillContext

_log = logging.getLogger("ars.skills")


class ToolNotFound(SkillError):
    pass


class UndeclaredCapability(RuntimeError):
    """A skill asked for a capability outside its own manifest. Hard failure, always."""


class InProcessSkillRuntime(SkillRuntimeInterface):
    """Runs first-party, trusted skills in-process.

    Third-party skills must NOT run here — they belong in the subprocess runtime, which
    is a separate implementation of the same interface. `register` refuses any manifest
    that is not marked trusted, so the distinction cannot be forgotten under deadline.
    """

    def __init__(
        self,
        *,
        guard_evaluate: Callable[[GuardQuery], Awaitable[Any]] | None = None,
        secret_provider: Callable[[str], Awaitable[str | None]] | None = None,
    ) -> None:
        self._skills: dict[str, Skill] = {}
        self._tool_index: dict[str, tuple[Skill, ToolSpec]] = {}
        self._guard_evaluate = guard_evaluate
        self._secret_provider = secret_provider or self._no_secrets

    @staticmethod
    async def _no_secrets(_name: str) -> str | None:
        return None

    def register(self, skill: Skill) -> None:
        m = skill.manifest
        if not m.trusted:
            raise ValueError(
                f"skill {m.name!r} is not trusted and must not run in-process; "
                "use the subprocess runtime"
            )
        declared = set(m.capabilities)
        for spec in m.tools:
            missing = set(spec.capabilities) - declared
            if missing:
                raise UndeclaredCapability(
                    f"tool {spec.name!r} in skill {m.name!r} uses undeclared capabilities: "
                    f"{sorted(c.value for c in missing)}"
                )
            if spec.name in self._tool_index:
                raise ValueError(f"duplicate tool name {spec.name!r}")
            self._tool_index[spec.name] = (skill, spec)
        self._skills[m.name] = skill

    async def available_tools(self) -> tuple[ToolSpec, ...]:
        return tuple(spec for _, spec in self._tool_index.values())

    def capabilities_for(self, tool: str) -> tuple[Capability, ...]:
        entry = self._tool_index.get(tool)
        return entry[1].capabilities if entry else ()

    async def invoke(self, call: ToolCall, *, timeout_s: float = 30.0,
                     guard_query: GuardQuery | None = None) -> ToolResult:
        started = time.monotonic()
        entry = self._tool_index.get(call.tool)
        if entry is None:
            return ToolResult(call_id=call.id, tool=call.tool, status=ToolStatus.ERROR,
                              error=f"no such tool: {call.tool}")
        skill, spec = entry

        # Defence in depth: re-run the guard on the capabilities this tool actually declares.
        if self._guard_evaluate is not None and guard_query is not None:
            for cap in spec.capabilities:
                decision = await self._guard_evaluate(
                    guard_query.model_copy(update={"capability": cap})
                )
                if decision.verdict is not Verdict.ALLOW:
                    return ToolResult(
                        call_id=call.id, tool=call.tool,
                        status=ToolStatus.DENIED if decision.verdict is Verdict.DENY
                        else ToolStatus.NEEDS_CONSENT,
                        error=decision.explanation,
                        duration_ms=int((time.monotonic() - started) * 1000),
                    )

        cancelled = asyncio.Event()
        http = MediatedHttp(skill.manifest.network_allowlist)
        ctx = SkillContext(http=http, language=skill.manifest.languages[0]
                           if skill.manifest.languages else __import__("ars_protocol").Language.EN,
                           cancelled=cancelled, secret=self._secret_provider)
        try:
            blocks = await asyncio.wait_for(skill.call(call.tool, call.arguments, ctx), timeout_s)
        except TimeoutError:
            return ToolResult(call_id=call.id, tool=call.tool, status=ToolStatus.TIMEOUT,
                              error=f"{call.tool} exceeded {timeout_s}s",
                              duration_ms=int((time.monotonic() - started) * 1000))
        except asyncio.CancelledError:
            return ToolResult(call_id=call.id, tool=call.tool, status=ToolStatus.CANCELLED,
                              duration_ms=int((time.monotonic() - started) * 1000))
        except httpx.HTTPError as e:
            # A network blip is not an internal error and must not be reported as one:
            # the user can act on "I could not reach it", and it tells them the failure
            # was not their assistant misbehaving.
            _log.info("network failure in %s: %s", call.tool, type(e).__name__)
            return ToolResult(
                call_id=call.id, tool=call.tool, status=ToolStatus.ERROR,
                error=f"could not reach the service {call.tool} needs "
                      f"({type(e).__name__}); it may be offline or blocking us",
                duration_ms=int((time.monotonic() - started) * 1000))
        except NetworkDenied as e:
            return ToolResult(call_id=call.id, tool=call.tool, status=ToolStatus.DENIED,
                              error=str(e), duration_ms=int((time.monotonic() - started) * 1000))
        except SkillError as e:
            return ToolResult(call_id=call.id, tool=call.tool, status=ToolStatus.ERROR,
                              error=str(e), duration_ms=int((time.monotonic() - started) * 1000))
        except Exception:
            # Never leak internals into the conversation — but never lose them either.
            # A bare "internal error" with no trace anywhere is how a broken skill
            # survives for weeks looking like a model quality problem.
            _log.exception("skill %s raised an unexpected error in tool %s",
                           skill.manifest.name, call.tool)
            return ToolResult(call_id=call.id, tool=call.tool, status=ToolStatus.ERROR,
                              error="internal error in skill",
                              duration_ms=int((time.monotonic() - started) * 1000))
        finally:
            await http.aclose()

        return skill.ok(call.id, call.tool, blocks,
                        int((time.monotonic() - started) * 1000))
