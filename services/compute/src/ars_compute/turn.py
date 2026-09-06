"""The turn loop.

    assemble context
      -> stream from the backend
        -> emit ReplyDelta as text arrives
        -> on a tool call: build a GuardQuery, evaluate, and only on ALLOW execute it
        -> feed the ToolResult back with its provenance intact, and go round again
      -> ReplyDone

Four properties this loop must have, and how each is achieved:

**Nothing acts without a guard ALLOW.** `SkillRuntime.invoke` is called from exactly one
place in this file, and the only way to reach it is through `_authorise`, which returns
ALLOW or nothing. There is no other call site.

**A tainted turn cannot act silently.** `GuardQuery.tainted` comes from the assembler's
provenance walk, never from a keyword scan. On top of the guard's verdict there is a local
backstop: if the guard returns ALLOW for a CRITICAL-risk capability inside a tainted turn,
compute refuses anyway and says so. Two layers written by different people have to agree
before anything irreversible happens.

**It is cancellable.** Barge-in has to stop the model, the tool and the reply. `cancel()`
sets an event that every await point in this file races against, and it propagates to the
backend so generation actually stops rather than merely being ignored.

**It starts speaking fast enough to be heard as an answer.** A reasoning model left at its
default spends seconds thinking before the first visible token, which on the voice path is
seconds of silence. `ThinkPolicy` decides per turn, and a spoken turn gets
`ReasoningMode.OFF`. This is enforced here rather than configured in the backend, because
it depends on how the turn arrived — the same model, same session, must think for a typed
question and not for a spoken one.

**It speaks within 600 ms of starting a tool call.** `docs/architecture/overview.md` gives
a turn with a tool call a filler acknowledgement deadline of 600 ms. TTS needs 120 ms of
that, so the filler text is emitted at `filler_at_ms` (300 ms by default), leaving
headroom. If the tool returns before then, nothing is said — a filler for a 40 ms cache
hit is noise.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from ars_core import GuardEngine, LlmBackend, SkillRuntime
from ars_protocol import (
    AgentState,
    Capability,
    ConsentRequired,
    ContentBlock,
    ErrorEvent,
    GrantRequest,
    GuardDecision,
    GuardQuery,
    Language,
    MemoryRecord,
    ObservationProposed,
    Preference,
    Provenance,
    ReplyDelta,
    ReplyDone,
    Risk,
    ServerEvent,
    Session,
    SourceKind,
    StateChanged,
    ToolCall,
    ToolCallEvent,
    ToolResult,
    ToolResultEvent,
    ToolSpec,
    ToolStatus,
    Transcript,
    TrustLevel,
    Turn,
    Verdict,
)

from . import prompts
from .backends.base import BaseBackend
from .backends.router import DEFAULT_THINK_POLICY, Router, ThinkPolicy
from .context import (
    PROV_ASSISTANT,
    AssembledContext,
    ContextAssembler,
    ContextBudget,
)
from .errors import (
    BackendUnavailable,
    CloudRoutingRefused,
    ContextOverflow,
    TaintBackstopTriggered,
)
from .language import DiacriticRepairStream, ReplyLanguage, resolve_reply_language
from .reasoning import ReasoningMode

RESOURCE_KEYS: tuple[str, ...] = (
    "url", "uri", "link", "path", "file", "filename", "repo", "repository",
    "to", "recipient", "address", "query", "q", "search", "text", "subject",
)
"""Argument names that name the concrete thing a tool touches, in priority order.
`GuardQuery.resource` is what grants are matched against, so guessing wrong here means
matching the wrong grant. Ordered so that an identifier (a URL, a path, a recipient) wins
over free text."""


@dataclass
class TurnStats:
    assembly_ms: float = 0.0
    first_delta_ms: float | None = None
    total_ms: float = 0.0
    rounds: int = 0
    tool_calls_proposed: int = 0
    tool_calls_executed: int = 0
    guard_queries: int = 0
    denied: int = 0
    filler_spoken: bool = False
    filler_at_ms: float | None = None
    reasoning: str = ""
    """The mode used on the first model round — the one the first-token budget is measured
    against. Reported so a latency regression can be traced to a policy change."""
    input_tokens: int = 0
    output_tokens: int = 0
    backend: str = ""
    model: str = ""
    ran_locally: bool = True
    est_cost_usd: float = 0.0
    dropped: int = 0
    prompt_refs: tuple[str, ...] = ()


@dataclass
class TurnOutcome:
    turn: Turn
    text: str = ""
    language: Language = Language.EN
    tainted: bool = False
    cancelled: bool = False
    awaiting_consent: bool = False
    injection_markers: tuple[str, ...] = ()
    stats: TurnStats = field(default_factory=TurnStats)


class TurnOrchestrator:
    def __init__(
        self,
        *,
        backend: LlmBackend,
        guard: GuardEngine,
        skills: SkillRuntime | None = None,
        assembler: ContextAssembler | None = None,
        budget: ContextBudget | None = None,
        max_tool_calls_per_turn: int = 8,
        filler_at_ms: float = 300.0,
        filler_deadline_ms: float = 600.0,
        tool_timeout_s: float = 30.0,
        think_policy: ThinkPolicy = DEFAULT_THINK_POLICY,
        announce_injections: bool = True,
        taint_backstop: bool = True,
        learner: object | None = None,
    ) -> None:
        self.backend = backend
        self.guard = guard
        self.skills = skills
        self.assembler = assembler or ContextAssembler(budget=budget or ContextBudget())
        self.max_tool_calls = max_tool_calls_per_turn
        self.filler_at_ms = filler_at_ms
        self.filler_deadline_ms = filler_deadline_ms
        self.tool_timeout_s = tool_timeout_s
        self.think_policy = think_policy
        self.announce_injections = announce_injections
        self.taint_backstop = taint_backstop
        self.learner = learner
        self._cancel = asyncio.Event()
        self.last: TurnOutcome | None = None

    # ------------------------------------------------------------------ cancel
    async def cancel(self) -> None:
        """Barge-in. Cancels the model stream, the running tool and the reply. Idempotent,
        because a user who interrupts twice is not an error case."""
        self._cancel.set()
        await self.backend.cancel()

    def reset(self) -> None:
        self._cancel = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # ------------------------------------------------------------------ run
    async def run(
        self,
        *,
        session: Session,
        transcript: Transcript,
        turn: Turn | None = None,
        history: tuple[ContentBlock, ...] = (),
        memories: tuple[MemoryRecord, ...] = (),
        preferences: tuple[Preference, ...] = (),
        tools: Sequence[ToolSpec] = (),
        spoken: bool = True,
    ) -> AsyncIterator[ServerEvent]:
        self.reset()
        t_start = time.perf_counter()
        turn = turn or Turn(session_id=session.id)
        tool_specs = tuple(tools)
        by_name = {t.name: t for t in tool_specs}
        language = resolve_reply_language(transcript, session=session, typed=not spoken)
        stats = TurnStats()
        outcome = TurnOutcome(turn=turn, language=language.language, stats=stats)
        self.last = outcome

        external: list[ContentBlock] = []
        tool_results: list[tuple[ContentBlock, str]] = []
        tool_calls: list[ToolCall] = []
        reply_parts: list[str] = []
        executed = 0

        yield StateChanged(state=AgentState.THINKING, turn_id=turn.id)

        try:
            for _round in range(self.max_tool_calls + 1):
                stats.rounds += 1
                ctx = self._assemble(
                    turn=turn, language=language, transcript=transcript, spoken=spoken,
                    history=history, memories=memories, preferences=preferences,
                    external=tuple(external), tool_results=tuple(tool_results),
                    tool_calls=tuple(tool_calls), tools=tool_specs,
                )
                stats.assembly_ms += ctx.assembly_ms
                stats.dropped += len(ctx.dropped)
                stats.prompt_refs = ctx.prompt_refs
                stats.input_tokens += ctx.est_input_tokens
                outcome.tainted = ctx.tainted
                outcome.injection_markers = ctx.injection_markers
                # `Turn.tainted` is frozen, so the turn is re-stamped rather than mutated.
                turn = Turn(id=turn.id, session_id=turn.session_id,
                            started_at_ms=turn.started_at_ms, tainted=ctx.tainted)

                reasoning = self.think_policy.decide(
                    spoken=spoken,
                    escalated=isinstance(self.backend, Router) and self.backend.preview(ctx),
                    round_index=_round,
                    filler_spoken=stats.filler_spoken,
                )
                if _round == 0:
                    stats.reasoning = reasoning.value

                pending: ToolCall | None = None
                repair = DiacriticRepairStream(language.language)

                async for piece in await self._start_stream(ctx, tool_specs, reasoning):
                    if self.cancelled:
                        break
                    if isinstance(piece, str):
                        if stats.first_delta_ms is None:
                            stats.first_delta_ms = (time.perf_counter() - t_start) * 1000.0
                        out = repair.push(piece)
                        if out:
                            reply_parts.append(out)
                            yield ReplyDelta(turn_id=turn.id, text=out,
                                             language=language.language)
                    else:
                        pending = piece
                        stats.tool_calls_proposed += 1
                        break

                tail = repair.flush()
                if tail:
                    reply_parts.append(tail)
                    yield ReplyDelta(turn_id=turn.id, text=tail, language=language.language)

                if self.cancelled:
                    outcome.cancelled = True
                    yield StateChanged(state=AgentState.IDLE, turn_id=turn.id)
                    return

                if pending is None:
                    break

                if executed >= self.max_tool_calls:
                    text = self._notice("tool_budget", language.language)
                    reply_parts.append(text)
                    yield ReplyDelta(turn_id=turn.id, text=text, language=language.language)
                    break

                spec = by_name.get(pending.tool)
                if spec is None:
                    result = ToolResult(
                        call_id=pending.id, tool=pending.tool, status=ToolStatus.ERROR,
                        error=f"unknown tool {pending.tool!r}",
                    )
                    yield ToolResultEvent(turn_id=turn.id, result=result)
                    tool_calls.append(pending)
                    tool_results.append((self._error_block(result), pending.id))
                    continue

                decision, query = await self._authorise(
                    session=session, turn=turn, call=pending, spec=spec, ctx=ctx,
                    language=language.language,
                )
                stats.guard_queries += 1
                yield ToolCallEvent(turn_id=turn.id, call=pending, decision=decision)

                if decision.verdict is Verdict.ASK:
                    yield StateChanged(state=AgentState.WAITING_FOR_CONSENT, turn_id=turn.id)
                    yield ConsentRequired(
                        turn_id=turn.id,
                        request=GrantRequest(
                            capability=decision.capability,
                            resource_patterns=(query.resource,) if query.resource else ("*",),
                            reason=query.summary,
                        ),
                        spoken_prompt=decision.explanation,
                    )
                    outcome.awaiting_consent = True
                    await self.guard.record_outcome(query, decision, "asked")
                    break

                if decision.verdict is Verdict.DENY:
                    stats.denied += 1
                    await self.guard.record_outcome(query, decision, "denied")
                    result = ToolResult(
                        call_id=pending.id, tool=pending.tool, status=ToolStatus.DENIED,
                        error=decision.reason.value if decision.reason else "denied",
                    )
                    yield ToolResultEvent(turn_id=turn.id, result=result)
                    text = self._notice(
                        "tool_denied", language.language,
                        summary=query.summary, explanation=decision.explanation,
                    )
                    reply_parts.append(text)
                    yield ReplyDelta(turn_id=turn.id, text=text, language=language.language)
                    tool_calls.append(pending)
                    tool_results.append((self._denied_block(result, decision), pending.id))
                    continue

                # ---- ALLOW ------------------------------------------------------
                yield StateChanged(state=AgentState.ACTING, turn_id=turn.id)
                filler = self._filler(spec, language.language, turn.id)
                async for event in self._run_tool_with_filler(
                    turn=turn, call=pending, spec=spec, filler=filler,
                    language=language.language, stats=stats, query=query,
                    decision=decision, sink=tool_results, calls=tool_calls,
                ):
                    if isinstance(event, ReplyDelta):
                        reply_parts.append(event.text)
                    if isinstance(event, ToolResultEvent) and event.result.content:
                        for text, prov in event.result.content:
                            if prov.trust is TrustLevel.EXTERNAL:
                                external.append(ContentBlock(text=text, provenance=prov))
                    yield event
                executed += 1
                stats.tool_calls_executed = executed

                if self.cancelled:
                    outcome.cancelled = True
                    yield StateChanged(state=AgentState.IDLE, turn_id=turn.id)
                    return
                yield StateChanged(state=AgentState.THINKING, turn_id=turn.id)

            if self.announce_injections and outcome.injection_markers:
                notice = self._notice(
                    "injection_detected", language.language,
                    source=_source_label(ctx),
                    phrases="; ".join(outcome.injection_markers[:2]),
                )
                reply_parts.append(" " + notice)
                yield ReplyDelta(turn_id=turn.id, text=" " + notice,
                                 language=language.language)

        except ContextOverflow:
            async for e in self._fail(turn, language.language, "context_overflow",
                                      "context_overflow", reply_parts):
                yield e
        except CloudRoutingRefused:
            async for e in self._fail(turn, language.language, "cloud_refused",
                                      "sensitive_content_not_routed", reply_parts):
                yield e
        except (BackendUnavailable, TaintBackstopTriggered) as exc:
            code = "taint_backstop" if isinstance(exc, TaintBackstopTriggered) else "backend_down"
            async for e in self._fail(turn, language.language, "backend_down", code, reply_parts):
                yield e
        except asyncio.CancelledError:
            outcome.cancelled = True
            yield StateChanged(state=AgentState.IDLE, turn_id=turn.id)
            raise

        text = "".join(reply_parts)
        outcome.text = text
        outcome.tainted = outcome.tainted or turn.tainted
        stats.total_ms = (time.perf_counter() - t_start) * 1000.0
        self._record_backend_stats(stats)

        yield ReplyDone(turn_id=turn.id, text=text, language=language.language)

        if self.learner is not None and not outcome.cancelled:
            for proposal in self._observations(session, turn, transcript, language.language):
                yield proposal

        yield StateChanged(state=AgentState.IDLE, turn_id=turn.id)

    # ------------------------------------------------------------------ pieces
    def _assemble(self, *, turn: Turn, language: ReplyLanguage, transcript: Transcript,
                  spoken: bool, history: tuple[ContentBlock, ...],
                  memories: tuple[MemoryRecord, ...], preferences: tuple[Preference, ...],
                  external: tuple[ContentBlock, ...],
                  tool_results: tuple[tuple[ContentBlock, str], ...],
                  tool_calls: tuple[ToolCall, ...],
                  tools: tuple[ToolSpec, ...]) -> AssembledContext:
        kwargs = dict(
            turn_id=turn.id, language=language, transcript=transcript, spoken=spoken,
            history=history, memories=memories, preferences=preferences, external=external,
            tool_results=tool_results, tool_calls=tool_calls, tools=tools,
            max_tool_calls=self.max_tool_calls,
        )
        ctx = self.assembler.assemble(**kwargs)  # type: ignore[arg-type]
        # If this is heading for a provider, rebuild it under the cloud budget so the
        # tighter memory allowance applies to what is actually transmitted, not to a
        # context we assembled and then discarded.
        if isinstance(self.backend, Router) and self.backend.preview(ctx):
            ctx = self.assembler.assemble(budget=self.assembler.budget.for_cloud(), **kwargs)  # type: ignore[arg-type]
        return ctx

    async def _start_stream(self, ctx: AssembledContext, tools: tuple[ToolSpec, ...],
                            reasoning: ReasoningMode) -> AsyncIterator[str | ToolCall]:
        fn = getattr(self.backend, "complete_context", None)
        if fn is not None:
            return await fn(ctx, tools=tools, reasoning=reasoning)
        return await self.backend.complete(
            system=ctx.system, context=ctx.blocks, tools=tools,
            language=ctx.reply_language, reasoning=reasoning,
        )

    async def _authorise(self, *, session: Session, turn: Turn, call: ToolCall,
                         spec: ToolSpec, ctx: AssembledContext,
                         language: Language) -> tuple[GuardDecision, GuardQuery]:
        """Every declared capability of the tool is evaluated, and the worst verdict wins.

        `GuardQuery` carries one capability, but a `ToolSpec` may declare several, and a
        tool that both reads mail and sends it must not be authorised on the strength of
        the read. DENY beats ASK beats ALLOW.
        """
        resource = _resource_of(call)
        if not spec.capabilities:
            # CLAUDE.md: "every tool call goes through GuardEngine.evaluate". A tool that
            # declares no capability cannot be evaluated, so it cannot run. This is a
            # manifest bug in the skill, surfaced rather than waved through.
            query = GuardQuery(
                session_id=session.id, turn_id=turn.id, capability=Capability.MEMORY_READ,
                resource=resource, summary=_summary(spec, None, resource, language),
                tainted=ctx.tainted, taint_sources=ctx.taint_sources,
            )
            return GuardDecision(
                verdict=Verdict.DENY, capability=Capability.MEMORY_READ,
                explanation=(
                    f"the skill providing {spec.name!r} declares no capability, so there is "
                    f"nothing for the guard to check against your grants"
                ),
            ), query

        worst: tuple[GuardDecision, GuardQuery] | None = None
        rank = {Verdict.ALLOW: 0, Verdict.ASK: 1, Verdict.DENY: 2}
        for capability in spec.capabilities:
            query = GuardQuery(
                session_id=session.id, turn_id=turn.id, capability=capability,
                resource=resource, summary=_summary(spec, capability, resource, language),
                tainted=ctx.tainted, taint_sources=ctx.taint_sources,
            )
            decision = await self.guard.evaluate(query)
            if worst is None or rank[decision.verdict] > rank[worst[0].verdict]:
                worst = (decision, query)
        assert worst is not None

        decision, query = worst
        if self.taint_backstop and decision.verdict is Verdict.ALLOW and ctx.tainted:
            decision = self._backstop(decision, query, spec, ctx, language)
        return decision, query

    def _backstop(self, decision: GuardDecision, query: GuardQuery, spec: ToolSpec,
                  ctx: AssembledContext, language: Language) -> GuardDecision:
        """Second opinion on an ALLOW inside a tainted turn. Two rules, two outcomes.

        **CRITICAL risk -> refuse outright.** `capability.py` says CRITICAL is "refused
        outright inside a tainted turn". If the guard allowed it anyway, two layers
        disagree about something irreversible, and the safe resolution is to stop.

        **Exfiltrating capability -> downgrade ALLOW to ASK.** A web search is harmless
        until the turn has read attacker-controlled text, at which point the attacker
        writes the query and `web.search` becomes a working exfiltration channel. The
        protocol's own remedy is to "surface the literal query to the user before it
        leaves", so that is what this does — ASK, with the exact resource quoted, not a
        model-written summary of it.
        """
        critical = next((c for c in spec.capabilities if c.risk is Risk.CRITICAL), None)
        if critical is not None:
            raise TaintBackstopTriggered(critical)

        exfil = next((c for c in spec.capabilities if c.exfiltrates_outward), None)
        if exfil is None:
            return decision
        return GuardDecision(
            verdict=Verdict.ASK, capability=exfil, reason=None,
            explanation=self._notice(
                "taint_exfiltration", language,
                source=_taint_label(ctx), summary=query.summary,
                resource=query.resource or "(no arguments)",
            ),
            matched_grant_id=decision.matched_grant_id, requires_confirmation=True,
        )

    async def _run_tool_with_filler(
        self, *, turn: Turn, call: ToolCall, spec: ToolSpec, filler: str,
        language: Language, stats: TurnStats, query: GuardQuery, decision: GuardDecision,
        sink: list[tuple[ContentBlock, str]], calls: list[ToolCall],
    ) -> AsyncIterator[ServerEvent]:
        if self.skills is None:
            result = ToolResult(call_id=call.id, tool=call.tool, status=ToolStatus.ERROR,
                                error="no skill runtime configured")
            await self.guard.record_outcome(query, decision, "error")
            calls.append(call)
            sink.append((self._error_block(result), call.id))
            yield ToolResultEvent(turn_id=turn.id, result=result)
            return

        t0 = time.perf_counter()
        task = asyncio.create_task(self.skills.invoke(call, timeout_s=self.tool_timeout_s))
        cancel_wait = asyncio.create_task(self._cancel.wait())

        # --- the 600 ms filler deadline -----------------------------------------
        done, _ = await asyncio.wait(
            {task, cancel_wait}, timeout=self.filler_at_ms / 1000,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task not in done and cancel_wait not in done and filler:
            stats.filler_spoken = True
            stats.filler_at_ms = (time.perf_counter() - t0) * 1000.0
            yield ReplyDelta(turn_id=turn.id, text=filler, language=language)

        if task not in done:
            done, _ = await asyncio.wait(
                {task, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
            )

        if task not in done:
            # Cancelled mid-tool. `SkillRuntime.invoke` is the last line before a side
            # effect, so we cancel the task rather than abandoning it.
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: S110
                # The turn is over either way; a skill that fails while being
                # cancelled has nothing left to tell the user.
                pass
            result = ToolResult(call_id=call.id, tool=call.tool, status=ToolStatus.CANCELLED,
                                duration_ms=int((time.perf_counter() - t0) * 1000))
            await self.guard.record_outcome(query, decision, "cancelled")
            yield ToolResultEvent(turn_id=turn.id, result=result)
            return

        cancel_wait.cancel()
        try:
            result = task.result()
        except Exception as exc:  # a skill crash must not kill the turn
            result = ToolResult(call_id=call.id, tool=call.tool, status=ToolStatus.ERROR,
                                error=f"{type(exc).__name__}: {exc}"[:200],
                                duration_ms=int((time.perf_counter() - t0) * 1000))

        await self.guard.record_outcome(query, decision, result.status.value)
        yield ToolResultEvent(turn_id=turn.id, result=result)

        calls.append(call)
        if result.status is ToolStatus.OK and result.content:
            # Provenance preserved exactly as the skill set it. This is the dotted line in
            # the architecture diagram: content re-entering the reasoning layer keeps its
            # trust level, so it will be quarantined and will taint the next round.
            for text, prov in result.content:
                sink.append((ContentBlock(text=text, provenance=prov), call.id))
        elif result.status is not ToolStatus.OK:
            sink.append((self._error_block(result), call.id))
            text = self._notice("tool_failed", language, summary=query.summary)
            yield ReplyDelta(turn_id=turn.id, text=text, language=language)

    # ------------------------------------------------------------------ helpers
    def _filler(self, spec: ToolSpec, language: Language, turn_id: str) -> str:
        table = prompts.fillers().get(language.value, {})
        caps = {c.value.split(".")[0] for c in spec.capabilities}
        for key in ("email", "files", "web"):
            if key in caps and table.get(key):
                options = table[key]
                break
        else:
            options = table.get("generic", [])
        if not options:
            return ""
        return options[sum(turn_id.encode()) % len(options)]

    def _notice(self, key: str, language: Language, **fields: object) -> str:
        entry = prompts.notices()[key][language.value]
        return str(entry["text"]).format(**fields)

    def _error_block(self, result: ToolResult) -> ContentBlock:
        return ContentBlock(
            text=f"[tool {result.tool} returned {result.status.value}: {result.error or ''}]",
            provenance=PROV_ASSISTANT,
        )

    def _denied_block(self, result: ToolResult, decision: GuardDecision) -> ContentBlock:
        return ContentBlock(
            text=(
                f"[the guard refused {result.tool}: {decision.explanation}. "
                f"Do not retry it. Tell the user and continue without it.]"
            ),
            provenance=PROV_ASSISTANT,
        )

    async def _fail(self, turn: Turn, language: Language, notice: str, code: str,
                    reply_parts: list[str]) -> AsyncIterator[ServerEvent]:
        text = self._notice(notice, language)
        reply_parts.append(text)
        yield ReplyDelta(turn_id=turn.id, text=text, language=language)
        yield ErrorEvent(code=code, message=text, turn_id=turn.id, recoverable=True)
        yield ReplyDone(turn_id=turn.id, text="".join(reply_parts), language=language)
        yield StateChanged(state=AgentState.IDLE, turn_id=turn.id)

    def _record_backend_stats(self, stats: TurnStats) -> None:
        backend = self.backend
        if isinstance(backend, Router):
            d = backend.last_decision
            if d is not None:
                stats.backend, stats.model, stats.ran_locally = d.backend, d.model, d.runs_locally
            backend = backend.active
        info = getattr(backend, "info", None)
        if info is not None:
            stats.backend = stats.backend or info.name
            stats.model = stats.model or info.model
            stats.ran_locally = info.runs_locally if not stats.backend else stats.ran_locally
        last = getattr(backend, "last_stats", None)
        if last is not None:
            stats.output_tokens += last.output_tokens
            if last.input_tokens:
                stats.input_tokens = last.input_tokens
        elif isinstance(backend, BaseBackend):
            all_stats = getattr(backend, "stats", None)
            if all_stats:
                stats.output_tokens += sum(s.output_tokens for s in all_stats[-stats.rounds:])
        if info is not None:
            stats.est_cost_usd = info.price.cost(stats.input_tokens, stats.output_tokens)

    def _observations(self, session: Session, turn: Turn, transcript: Transcript,
                      language: Language) -> list[ObservationProposed]:
        propose = getattr(self.learner, "propose", None)
        if propose is None:
            return []
        out: list[ObservationProposed] = []
        for observation, spoken in propose(
            turn_id=turn.id, text=transcript.text, language=language
        ):
            out.append(ObservationProposed(observation=observation, spoken_prompt=spoken))
        return out


def _resource_of(call: ToolCall) -> str | None:
    for key in RESOURCE_KEYS:
        value = call.arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:512]
    return None


def _summary(spec: ToolSpec, capability: Capability | None, resource: str | None,
             language: Language) -> str:
    table = prompts.guard_summaries()
    entry = None
    if capability is not None:
        entry = (table.get("capability", {}).get(capability.value, {}) or {}).get(language.value)
    if entry is None:
        entry = table["default"][language.value]
    key = "with_resource" if resource else "without_resource"
    return str(entry[key]).format(tool=spec.name, resource=resource or "")


def _taint_label(ctx: AssembledContext) -> str:
    for p in ctx.taint_sources:
        return p.label or p.uri or p.source.value
    return "an outside source"


def _source_label(ctx: AssembledContext) -> str:
    for item in ctx.items:
        if item.injection_markers:
            p: Provenance = item.block.provenance
            return p.label or p.uri or p.source.value
    return SourceKind.SKILL_OUTPUT.value


__all__ = ["RESOURCE_KEYS", "TurnOrchestrator", "TurnOutcome", "TurnStats"]
