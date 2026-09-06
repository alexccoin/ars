"""Failure modes of the reasoning layer.

Everything here is a *refusal*, not a crash. The distinction matters: a refusal is a
result the orchestrator can turn into something the user hears, in their language. An
exception that escapes to the gateway becomes a spinner that never stops.
"""

from __future__ import annotations

from ars_protocol import Capability, Language, Provenance


class ComputeError(Exception):
    """Base for everything raised inside services/compute."""


class BackendUnavailable(ComputeError):
    """The backend could not be reached or refused the request. Recoverable: the router
    may fall back, or the turn ends with a spoken apology."""

    def __init__(self, backend: str, detail: str) -> None:
        super().__init__(f"{backend}: {detail}")
        self.backend = backend
        self.detail = detail


class CloudRoutingRefused(ComputeError):
    """A cloud backend was selected while SENSITIVE content was in the context.

    This is a hard stop, never a downgrade-and-continue. The architecture doc says
    "SENSITIVE data never leaves the device regardless of configuration"; a soft
    preference that can be overridden by a config flag is not that guarantee.

    The exception deliberately carries provenance, never the offending text — an error
    message is a log line, and a log line must not become a second copy of the secret.
    """

    def __init__(self, backend: str, sources: tuple[Provenance, ...], rule: str) -> None:
        where = ", ".join(
            f"{p.source.value}:{p.uri or p.label or '?'}" for p in sources
        ) or "unknown"
        super().__init__(
            f"refusing to route to non-local backend {backend!r}: sensitive content in "
            f"context (matched rule {rule!r}, from {where})"
        )
        self.backend = backend
        self.sources = sources
        self.rule = rule


class NoBackendAvailable(ComputeError):
    """Local backend is down and policy forbids the cloud (or there is no cloud backend).
    A.R.S is local-first: this is an expected state on a laptop with the model unloaded,
    not an exceptional one."""


class ContextOverflow(ComputeError):
    """Even after dropping everything droppable, the mandatory sections do not fit.
    Means the user's own utterance plus the system prompt exceed the window — the only
    honest response is to ask them to shorten it."""

    def __init__(self, needed: int, budget: int) -> None:
        super().__init__(f"mandatory context needs {needed} tokens, budget is {budget}")
        self.needed = needed
        self.budget = budget


class ToolBudgetExceeded(ComputeError):
    """`max_tool_calls_per_turn` reached. Prevents a runaway agent loop touching real
    accounts, which is the failure mode GuardConfig calls out by name."""

    def __init__(self, limit: int) -> None:
        super().__init__(f"tool call budget of {limit} exhausted in this turn")
        self.limit = limit


class TaintBackstopTriggered(ComputeError):
    """The guard returned ALLOW for a CRITICAL-risk capability inside a tainted turn.

    Defence in depth. The guard is another team's code and may have a bug or a
    misconfigured grant; CLAUDE.md rule 4 says a tainted turn blocks silent use of
    effectful capabilities, so compute refuses too rather than trusting a single check.
    Logged loudly, because it means two layers disagree.
    """

    def __init__(self, capability: Capability) -> None:
        super().__init__(
            f"guard allowed {capability.value} ({capability.risk.value} risk) in a tainted "
            f"turn; compute refuses anyway"
        )
        self.capability = capability


class TurnCancelled(ComputeError):
    """Barge-in or explicit cancel. Not an error the user ever hears about."""


class PromptAssetMissing(ComputeError):
    def __init__(self, asset_id: str, language: Language | None) -> None:
        super().__init__(f"no prompt asset {asset_id!r} for language {language}")
