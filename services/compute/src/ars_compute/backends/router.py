"""Choosing a backend. Local by default; cloud only when it is provably safe.

The policy is short enough to state completely:

  1. Local is the default in every policy. A.R.S is a local-first assistant, and the
     reference deployment (`docs/architecture/overview.md`, "Laptop standalone") needs no
     network at all.
  2. A non-local backend may be selected only if the policy permits it AND the local
     backend is genuinely unavailable or the turn was explicitly escalated.
  3. A non-local backend may **never** be selected while `Sensitivity.SENSITIVE` content
     is in the context. This is a check that raises, not a preference that loses a
     tie-break. `CloudRoutingRefused` propagates; there is no code path that catches it
     and downgrades to "well, send it anyway".

Rule 3 is written as a raise for a specific reason. A soft preference is one bad `or` away
from being wrong, and the failure is silent and unrecoverable — the data is already at the
provider. An exception has to be caught deliberately by someone who has to explain why,
and no such catch exists in this codebase.

The sensitivity check runs **twice**: once on what the assembler declared (authoritative,
from `MemoryRecord.sensitivity`), once as a fresh scan of every block's text immediately
before dispatch (catches blocks that never went through the assembler, such as a tool
result added mid-turn). Either one firing is enough to refuse.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from enum import StrEnum

from ars_core import LlmBackend
from ars_protocol import ContentBlock, Language, ToolCall, ToolSpec, TrustLevel

from ..context import URI_ROUTING_HINT, AssembledContext
from ..errors import CloudRoutingRefused, NoBackendAvailable
from ..sensitivity import (
    DEFAULT_CLASSIFIER,
    SensitivityClassifier,
    SensitivityFinding,
    SensitivityLedger,
)

ESCALATION_TOKEN = "escalate=cloud"  # noqa: S105 - a routing marker, not a credential
"""The only way a turn asks for the cloud. It must appear in a `TrustLevel.SYSTEM` block
whose uri is `ars:routing-hint`, which only the orchestrator constructs. An EXTERNAL block
containing this string is inert: the trust check below rejects it before the text is even
looked at, so a scraped page cannot escalate its own turn off the device."""


class RoutingPolicy(StrEnum):
    LOCAL_ONLY = "local_only"
    """Default. There is no cloud path at all. The only configuration that makes the
    privacy claim unconditionally true."""

    PREFER_LOCAL = "prefer_local"
    """Local unless the turn was explicitly escalated or the local model is down."""

    PREFER_CLOUD = "prefer_cloud"
    """Cloud for reasoning, local as fallback. Still refuses SENSITIVE content."""


@dataclass(frozen=True)
class RoutingDecision:
    backend: str
    model: str
    runs_locally: bool
    policy: RoutingPolicy
    reason: str
    escalated: bool = False
    sensitive_findings: tuple[SensitivityFinding, ...] = ()

    @property
    def left_the_device(self) -> bool:
        return not self.runs_locally


def escalation_requested(blocks: Sequence[ContentBlock]) -> bool:
    return any(
        b.provenance.trust is TrustLevel.SYSTEM
        and b.provenance.uri == URI_ROUTING_HINT
        and ESCALATION_TOKEN in b.text
        for b in blocks
    )


class Router(LlmBackend):
    """An `LlmBackend` that is really a policy. Callers cannot tell the difference, which
    is the point: routing local vs cloud is a config change, never a code change."""

    def __init__(
        self,
        *,
        local: LlmBackend,
        cloud: LlmBackend | None = None,
        policy: RoutingPolicy = RoutingPolicy.LOCAL_ONLY,
        classifier: SensitivityClassifier = DEFAULT_CLASSIFIER,
    ) -> None:
        if cloud is not None and cloud.runs_locally:
            raise ValueError(
                "the `cloud` slot must hold a backend with runs_locally=False; passing a "
                "local backend here would make the sensitivity check vacuous"
            )
        self._local = local
        self._cloud = cloud
        self.policy = policy
        self._classifier = classifier
        self.last_decision: RoutingDecision | None = None
        self._active: LlmBackend = local

    # ---------------------------------------------------------------- interface
    @property
    def runs_locally(self) -> bool:
        """True only when NO configuration of this router can send data off the device.

        Deliberately not "whatever the last chosen backend was". The guard reads this to
        decide whether SENSITIVE content may be in play at all, and the honest answer to
        "can user data leave the device when this backend is used" is yes as soon as a
        cloud backend is wired in with a permissive policy.
        """
        return self._cloud is None or self.policy is RoutingPolicy.LOCAL_ONLY

    async def cancel(self) -> None:
        await self._local.cancel()
        if self._cloud is not None:
            await self._cloud.cancel()

    # ---------------------------------------------------------------- the check
    def _sensitive(self, blocks: tuple[ContentBlock, ...],
                   ledger: SensitivityLedger | None) -> tuple[SensitivityFinding, ...]:
        led = ledger or SensitivityLedger(classifier=self._classifier)
        return led.scan(blocks)

    def _guard_cloud(self, backend: LlmBackend, blocks: tuple[ContentBlock, ...],
                     ledger: SensitivityLedger | None) -> tuple[SensitivityFinding, ...]:
        findings = self._sensitive(blocks, ledger)
        if findings and not backend.runs_locally:
            raise CloudRoutingRefused(
                getattr(getattr(backend, "info", None), "name", type(backend).__name__),
                tuple(f.provenance for f in findings),
                findings[0].rule or "declared",
            )
        return findings

    async def _local_up(self) -> bool:
        probe = getattr(self._local, "available", None)
        if probe is None:
            return True
        try:
            return bool(await probe())
        except Exception:  # a probe that raises means "down"
            return False

    # ---------------------------------------------------------------- choosing
    async def choose(
        self, blocks: tuple[ContentBlock, ...], *,
        ledger: SensitivityLedger | None = None,
        escalate: bool | None = None,
    ) -> tuple[LlmBackend, RoutingDecision]:
        wants_cloud = escalation_requested(blocks) if escalate is None else escalate

        Chosen = tuple[LlmBackend, RoutingDecision]

        def decide(backend: LlmBackend, reason: str,
                   findings: tuple[SensitivityFinding, ...] = ()) -> Chosen:
            info = getattr(backend, "info", None)
            d = RoutingDecision(
                backend=getattr(info, "name", type(backend).__name__),
                model=getattr(info, "model", "?"), runs_locally=backend.runs_locally,
                policy=self.policy, reason=reason, escalated=wants_cloud,
                sensitive_findings=findings,
            )
            self.last_decision = d
            self._active = backend
            return backend, d

        if self._cloud is None or self.policy is RoutingPolicy.LOCAL_ONLY:
            if not await self._local_up():
                raise NoBackendAvailable(
                    "local backend is unavailable and policy is local-only; "
                    "A.R.S will not silently route this turn to a provider"
                )
            return decide(self._local, "policy=local_only")

        if self.policy is RoutingPolicy.PREFER_CLOUD:
            findings = self._guard_cloud(self._cloud, blocks, ledger)
            return decide(self._cloud, "policy=prefer_cloud", findings)

        # PREFER_LOCAL
        if wants_cloud:
            findings = self._guard_cloud(self._cloud, blocks, ledger)
            return decide(self._cloud, "escalated by orchestrator", findings)
        if not await self._local_up():
            findings = self._guard_cloud(self._cloud, blocks, ledger)
            return decide(self._cloud, "local backend unavailable", findings)
        return decide(self._local, "policy=prefer_local")

    def preview(self, ctx: AssembledContext) -> bool:
        """Would this context go to the cloud? Lets the orchestrator re-assemble with the
        tighter cloud budget *before* dispatch, so the privacy rule "never send more user
        memory than the task needs" is applied to what is actually sent."""
        if self._cloud is None or self.policy is RoutingPolicy.LOCAL_ONLY:
            return False
        if self.policy is RoutingPolicy.PREFER_CLOUD:
            return True
        return escalation_requested(ctx.blocks)

    # ---------------------------------------------------------------- dispatch
    async def complete(
        self, *, system: str, context: Sequence[ContentBlock],
        tools: Sequence[ToolSpec] = (), language: Language,
    ) -> AsyncIterator[str | ToolCall]:
        blocks = tuple(context)
        backend, _ = await self.choose(blocks)
        return await backend.complete(
            system=system, context=blocks, tools=tools, language=language
        )

    async def complete_context(
        self, ctx: AssembledContext, *, tools: Sequence[ToolSpec] = ()
    ) -> AsyncIterator[str | ToolCall]:
        backend, _ = await self.choose(ctx.blocks, ledger=ctx.ledger)
        fn = getattr(backend, "complete_context", None)
        if fn is not None:
            return await fn(ctx, tools=tools)
        return await backend.complete(
            system=ctx.system, context=ctx.blocks, tools=tools, language=ctx.reply_language
        )

    @property
    def active(self) -> LlmBackend:
        return self._active


__all__ = ["ESCALATION_TOKEN", "Router", "RoutingDecision", "RoutingPolicy", "escalation_requested"]
