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

This module also owns the *reasoning* policy — how much the model may think before it
speaks. That is the same kind of decision as local-vs-cloud (buy quality with latency, or
latency with quality), it is made per turn from the same information, and it belongs in
the same place. See `ThinkPolicy` and `ars_compute.reasoning`.

The sensitivity check runs **twice**: once on what the assembler declared (authoritative,
from `MemoryRecord.sensitivity`), once as a fresh scan of every block's text immediately
before dispatch (catches blocks that never went through the assembler, such as a tool
result added mid-turn). Either one firing is enough to refuse.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache

from ars_core import LlmBackend
from ars_protocol import ContentBlock, Language, ToolCall, ToolSpec, TrustLevel

from ..context import URI_ROUTING_HINT, AssembledContext
from ..errors import CloudRoutingRefused, NoBackendAvailable
from ..reasoning import ReasoningMode
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
class ThinkPolicy:
    """How much the model may think, decided per turn.

    The rule that does the work is the first one: **a spoken turn never thinks.** The user
    is sitting in silence waiting for audio, the budget to first token is 200 ms, and
    thinking costs ~2.8 s on the local model (`ars_compute.reasoning` has the numbers).
    There is no version of that trade which is worth making while someone is listening to
    nothing.

    Everything else can afford it:

    * **Typed** turns have no first-audio budget. Nobody is listening to silence; they are
      looking at a screen that can show a "thinking" state honestly. Quality wins.
    * **Escalated** turns are, by definition, ones the router already judged hard enough to
      be worth spending on — the same judgement that sends a turn to the cloud says it is
      worth thinking about.
    * **Later rounds of a tool loop**, once the filler acknowledgement has been spoken.
      This one is deliberately conservative and defaults to OFF: the architecture doc gives
      a tool-calling turn its own budget, but the user is still waiting for an answer after
      the tool returns, and 2.8 s of silence there is 2.8 s of silence. Available, off.

    Every field is a `ReasoningMode` rather than a bool so that a deployment with a model
    whose intermediate levels actually work can use them without touching this code.
    """

    spoken: ReasoningMode = ReasoningMode.OFF
    """Hard requirement, not a preference. Overriding this to a thinking mode is how the
    voice pipeline stops meeting its budget, so it is worth a review comment."""

    typed: ReasoningMode = ReasoningMode.ON
    escalated: ReasoningMode = ReasoningMode.ON
    """Typed turns only. A spoken turn is governed by `spoken` / `spoken_after_filler`
    whether or not it was escalated."""

    spoken_after_filler: ReasoningMode = ReasoningMode.OFF
    """Applies from the second model round of a spoken turn, once a filler has been
    spoken. Defaults to OFF; see the class docstring."""

    def decide(
        self,
        *,
        spoken: bool,
        escalated: bool = False,
        round_index: int = 0,
        filler_spoken: bool = False,
    ) -> ReasoningMode:
        if not spoken:
            return self.escalated if escalated else self.typed
        # Spoken from here down. Escalation deliberately has no say: it means the question
        # is hard, not that the user stopped waiting for a voice to start. `spoken_after
        # _filler` is the single, explicit knob by which a spoken turn may ever think, so
        # there is exactly one line to read when asking "can this path be slow".
        if round_index > 0 and filler_spoken:
            return self.spoken_after_filler
        return self.spoken


DEFAULT_THINK_POLICY = ThinkPolicy()


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


async def _dispatch(backend: LlmBackend, *, system: str, context: tuple[ContentBlock, ...],
                    tools: Sequence[ToolSpec], language: Language,
                    reasoning: ReasoningMode) -> AsyncIterator[str | ToolCall]:
    """Call `complete()` on any `LlmBackend`, passing `reasoning` only if it takes it.

    `reasoning` is an addition of ours; a third-party backend implementing the bare
    `packages/core` interface will not have the parameter, and dropping it is the right
    degradation — that backend has no thinking to switch off.

    The check is on the signature, not a `TypeError` around the call. Catching `TypeError`
    would also catch one raised *inside* the backend and then silently retry the request,
    which on an effectful path is how you send an email twice.
    """
    if _takes_reasoning(type(backend)):
        return await backend.complete(system=system, context=context, tools=tools,
                                      language=language, reasoning=reasoning)
    return await backend.complete(system=system, context=context, tools=tools,
                                  language=language)


@lru_cache(maxsize=64)
def _takes_reasoning(backend_type: type) -> bool:
    try:
        return "reasoning" in inspect.signature(backend_type.complete).parameters
    except (TypeError, ValueError):
        return False


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
        reasoning: ReasoningMode = ReasoningMode.OFF,
    ) -> AsyncIterator[str | ToolCall]:
        blocks = tuple(context)
        backend, _ = await self.choose(blocks)
        return await _dispatch(backend, system=system, context=blocks, tools=tools,
                               language=language, reasoning=reasoning)

    async def complete_context(
        self, ctx: AssembledContext, *, tools: Sequence[ToolSpec] = (),
        reasoning: ReasoningMode = ReasoningMode.OFF,
    ) -> AsyncIterator[str | ToolCall]:
        backend, _ = await self.choose(ctx.blocks, ledger=ctx.ledger)
        fn = getattr(backend, "complete_context", None)
        if fn is not None:
            return await fn(ctx, tools=tools, reasoning=reasoning)
        return await _dispatch(backend, system=ctx.system, context=ctx.blocks, tools=tools,
                               language=ctx.reply_language, reasoning=reasoning)

    @property
    def active(self) -> LlmBackend:
        return self._active


__all__ = [
    "DEFAULT_THINK_POLICY", "ESCALATION_TOKEN", "ReasoningMode", "Router",
    "RoutingDecision", "RoutingPolicy", "ThinkPolicy", "escalation_requested",
]
