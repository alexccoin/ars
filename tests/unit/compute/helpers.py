"""Fakes for the compute unit tests.

The guard and the skills runtime are being built by other agents. These stand in for
them at the interface boundary and, more usefully, *record* what they were asked — most
of the assertions in this directory are about what compute submitted to the guard and
whether the runtime was reached at all, not about what the guard decided.
"""

from __future__ import annotations

import asyncio

from ars_core import GuardEngine, SkillRuntime
from ars_protocol import (
    Capability,
    ContentBlock,
    DenyReason,
    GuardDecision,
    GuardQuery,
    Language,
    Provenance,
    SourceKind,
    ToolCall,
    ToolParam,
    ToolResult,
    ToolSpec,
    ToolStatus,
    Transcript,
    TrustLevel,
    Verdict,
)


class FakeGuard(GuardEngine):
    """Returns a scripted verdict and remembers every query it saw."""

    def __init__(self, verdict: Verdict = Verdict.ALLOW,
                 reason: DenyReason | None = None,
                 by_capability: dict[Capability, Verdict] | None = None) -> None:
        self.verdict = verdict
        self.reason = reason
        self.by_capability = by_capability or {}
        self.queries: list[GuardQuery] = []
        self.outcomes: list[tuple[GuardQuery, GuardDecision, str]] = []

    async def evaluate(self, query: GuardQuery) -> GuardDecision:
        self.queries.append(query)
        verdict = self.by_capability.get(query.capability, self.verdict)
        return GuardDecision(
            verdict=verdict, capability=query.capability,
            reason=self.reason if verdict is Verdict.DENY else None,
            explanation=f"[guard-composed] {verdict.value} for {query.capability.value}",
        )

    async def record_outcome(self, query, decision, outcome, user_confirmed=None) -> None:
        self.outcomes.append((query, decision, outcome))

    @property
    def tainted_queries(self) -> list[GuardQuery]:
        return [q for q in self.queries if q.tainted]


class FakeSkills(SkillRuntime):
    """Records invocations. `delay_s` lets a test hold a tool open long enough to observe
    the filler deadline or a barge-in."""

    def __init__(self, tools: tuple[ToolSpec, ...] = (), *, delay_s: float = 0.0,
                 result: ToolResult | None = None,
                 content: tuple[tuple[str, Provenance], ...] | None = None,
                 raises: Exception | None = None) -> None:
        self._tools = tools
        self.delay_s = delay_s
        self._result = result
        self._content = content
        self._raises = raises
        self.invocations: list[ToolCall] = []
        self.cancelled = False

    async def available_tools(self) -> tuple[ToolSpec, ...]:
        return self._tools

    async def invoke(self, call: ToolCall, *, timeout_s: float = 30.0) -> ToolResult:
        self.invocations.append(call)
        try:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self._raises is not None:
            raise self._raises
        if self._result is not None:
            return self._result
        return ToolResult(
            call_id=call.id, tool=call.tool, status=ToolStatus.OK,
            content=self._content or (("(empty)", EXTERNAL_PROV),),
        )


EXTERNAL_PROV = Provenance(
    source=SourceKind.WEB_PAGE, trust=TrustLevel.EXTERNAL,
    uri="https://example.com/page", label="web page at example.com",
)
EMAIL_PROV = Provenance(
    source=SourceKind.EMAIL, trust=TrustLevel.EXTERNAL,
    uri="msg-id:<abc@mail>", label="email from unknown@example.com",
)


def external(text: str, prov: Provenance = EXTERNAL_PROV) -> ContentBlock:
    return ContentBlock(text=text, provenance=prov)


WEB_FETCH = ToolSpec(
    name="web_fetch", description="Fetch the text of a web page.",
    params=(ToolParam(name="url", type="url", description="Page to fetch."),),
    capabilities=(Capability.WEB_FETCH,), returns_external_content=True,
)
EMAIL_SEND = ToolSpec(
    name="email_send", description="Send an email on the user's behalf.",
    params=(
        ToolParam(name="to", type="email", description="Recipient."),
        ToolParam(name="body", type="text", description="Body."),
    ),
    capabilities=(Capability.EMAIL_SEND,),
)
EMAIL_SEARCH = ToolSpec(
    name="email_search", description="Search the user's mailbox.",
    params=(ToolParam(name="query", type="string", description="Search query."),),
    capabilities=(Capability.EMAIL_SEARCH,), returns_external_content=True,
)


def utterance(text: str, language: Language = Language.EN,
              confidence: float = 0.95) -> Transcript:
    return Transcript(text=text, language=language, language_confidence=confidence,
                      is_final=True)


CALENDAR_READ = ToolSpec(
    name="calendar_read", description="Read the user's calendar.",
    params=(ToolParam(name="query", type="string", description="Date range."),),
    capabilities=(Capability.CALENDAR_READ,),
)
"""Neither effectful nor exfiltrating, and its output is USER_DATA rather than EXTERNAL,
so it does not taint the turn. Used where a test needs to loop without tripping the
taint backstop."""

CALENDAR_PROV = Provenance(
    source=SourceKind.MEMORY, trust=TrustLevel.USER_DATA, label="your calendar",
)
