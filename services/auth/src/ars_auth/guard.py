"""PolicyGuardEngine — allow, ask, or refuse.

This is the class the rest of A.R.S is built around. Every tool call passes through
:meth:`PolicyGuardEngine.evaluate` before any side effect exists, and the answer it gives
is the whole of the system's security posture.

The pipeline
------------
Six stages, in this order, each of which may only make the answer **more** restrictive
than the stage before it:

1. **Guard disabled.** If the kill switch is off, anything that touches private data or
   changes the world is DENY(``guard_disabled_capability``). Never a silent allow - the
   kill switch removes access, it does not remove checking.
2. **Grant resolution.** No grant at all for this capability: ASK if the capability may
   be requested interactively, otherwise DENY(``no_grant``). Grants exist but all of them
   are revoked: DENY(``grant_revoked``). All expired: DENY(``grant_expired``).
3. **Resource scope.** An active grant exists for the capability but none of them cover
   this resource: DENY(``resource_out_of_scope``). A grant is never widened to fit the
   call in front of it.
4. **Taint.** If the turn consumed untrusted content and the capability touches private
   data or is effectful, it can never end in a silent ALLOW. CRITICAL risk is
   DENY(``tainted_turn``) outright. Everything else becomes ASK, with a prompt the guard
   composed, naming where the untrusted text came from.
5. **Confirm policy.** ``EVERY_USE`` asks. ``ONCE_PER_SESSION`` asks the first time in a
   session and allows afterwards. ``NEVER`` allows, but only for LOW-risk capabilities.
6. **Rate limit.** Per capability, per turn and per session. Only calls that were about
   to be allowed or asked about consume budget.

Because each stage can only lower the verdict (ALLOW > ASK > DENY) and never raise it,
there is no ordering of stages that can produce more access than the most restrictive
stage permits. That property is what makes this readable as security: you do not have to
trace the interaction between rules, only find the strictest one that applies.

Two things this file refuses to do
----------------------------------
It never puts model-written text into a consent prompt for a tainted turn. ``summary``
comes from the reasoning layer, and in a tainted turn the reasoning layer has been
reading an attacker's text; letting it write the sentence the user approves would hand
the attacker the consent dialog. In a tainted turn the entire explanation comes from
:mod:`ars_auth.messages`.

It never performs I/O on the decision path except the audit write. Grants are served from
the store's in-memory cache; the architecture doc allocates the guard 0 ms and says that
if it needs network I/O the design is wrong. The audit write is the deliberate exception,
because a decision nobody can see afterwards is not a decision.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from ars_core.config import GuardConfig
from ars_core.interfaces import GuardEngine
from ars_protocol import (
    AuditRecord,
    CapabilityGrant,
    ConfirmPolicy,
    DenyReason,
    GrantRequest,
    GuardDecision,
    GuardQuery,
    Language,
    Provenance,
    Risk,
    TrustLevel,
    Verdict,
    now_ms,
)

from .audit import AuditLog
from .messages import (
    Msg,
    any_resource,
    capability_name,
    render,
    safe_fragment,
    source_name,
    unknown_origin,
    unknown_source,
)
from .policy import (
    exfiltrates,
    is_interactively_grantable,
    sensitive,
    silent_use_permitted,
)
from .session import RateLimiter, RateLimitPolicy, SessionConsentLedger
from .store import SqliteGrantStore

__all__ = ["PolicyGuardEngine"]

_RANK: Final[dict[Verdict, int]] = {Verdict.DENY: 0, Verdict.ASK: 1, Verdict.ALLOW: 2}

_TAINT_PRIORITY: Final = 10
"""Explanation priority for the taint stage. When two stages land on the same verdict,
the taint warning is the one the user hears - "may I read your calendar" and "may I read
your calendar, because a web page asked me to" are different questions."""

MAX_PENDING_DECISIONS: Final = 512


@dataclass
class _Proposal:
    """A verdict in progress. Only ever lowered, never raised."""

    verdict: Verdict
    key: Msg
    reason: DenyReason | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    priority: int = 0

    def lower_to(self, verdict: Verdict, key: Msg, *, reason: DenyReason | None = None,
                 fields: dict[str, Any] | None = None, priority: int = 0) -> None:
        new_rank, cur_rank = _RANK[verdict], _RANK[self.verdict]
        if new_rank > cur_rank:
            return  # a stage may never widen
        if new_rank < cur_rank or priority > self.priority:
            self.verdict = verdict
            self.key = key
            self.reason = reason
            self.priority = priority
            self.fields.update(fields or {})
        elif fields:
            self.fields.update(fields)


class PolicyGuardEngine(GuardEngine):
    """The guard. See the module docstring for the decision order."""

    def __init__(
        self,
        *,
        grants: SqliteGrantStore,
        audit: AuditLog,
        config: GuardConfig | None = None,
        limits: RateLimitPolicy | None = None,
        language: Language = Language.EN,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        self.grants = grants
        self.audit = audit
        self.config = config or GuardConfig()
        self.consents = SessionConsentLedger()
        self.limiter = RateLimiter(limits)
        self.default_language = language
        self._clock = clock
        self._session_language: OrderedDict[str, Language] = OrderedDict()
        self._pending: OrderedDict[tuple[Any, ...], str] = OrderedDict()

    # ------------------------------------------------------------------ language

    def set_session_language(self, session_id: str, language: Language) -> None:
        """Bilingual by default: the guard speaks whatever the user was speaking.

        The gateway calls this from the ASR's detected language. Everything the guard
        says exists in both languages, so this only ever chooses between ready strings.
        """
        self._session_language[session_id] = language
        self._session_language.move_to_end(session_id)
        while len(self._session_language) > 256:
            self._session_language.popitem(last=False)

    def language_for(self, session_id: str) -> Language:
        return self._session_language.get(session_id, self.default_language)

    # ------------------------------------------------------------------ evaluate

    async def evaluate(self, query: GuardQuery) -> GuardDecision:
        cap = query.capability
        lang = self.language_for(query.session_id)
        at = self._clock()

        proposal = _Proposal(verdict=Verdict.ALLOW, key=Msg.ALLOW_SILENT)
        grant: CapabilityGrant | None = None
        scope_patterns: tuple[str, ...] = ()

        # --- stage 1: global kill switch ------------------------------------------
        if not self.config.guard_enabled and sensitive(cap):
            return await self._finish(
                query, lang, at,
                _Proposal(Verdict.DENY, Msg.DENY_GUARD_DISABLED,
                          reason=DenyReason.GUARD_DISABLED_CAPABILITY),
                None,
            )

        # --- stage 2: is there a grant at all? ------------------------------------
        candidates = await self.grants.grants_for(cap)
        if not candidates:
            if is_interactively_grantable(cap):
                proposal.lower_to(Verdict.ASK, Msg.ASK_FIRST_USE)
            else:
                return await self._finish(
                    query, lang, at,
                    _Proposal(Verdict.DENY, Msg.DENY_NO_GRANT, reason=DenyReason.NO_GRANT),
                    None,
                )
        else:
            active = [g for g in candidates if g.is_active(at)]
            if not active:
                # Deliberate: an expired or revoked grant DENIES, it does not re-ask.
                # Re-asking after a revocation is how an attacker who has tainted a turn
                # farms the user for a fresh grant - the user already made this decision
                # once, and undoing it belongs on a permissions screen, not in a task.
                revoked = any(g.revoked_at_ms is not None for g in candidates)
                return await self._finish(
                    query, lang, at,
                    _Proposal(
                        Verdict.DENY,
                        Msg.DENY_GRANT_REVOKED if revoked else Msg.DENY_GRANT_EXPIRED,
                        reason=(DenyReason.GRANT_REVOKED if revoked
                                else DenyReason.GRANT_EXPIRED),
                    ),
                    None,
                )

            # --- stage 3: does any active grant cover this resource? --------------
            grant = await self.grants.find(cap, query.resource)
            if grant is None:
                scope_patterns = tuple(
                    dict.fromkeys(p for g in active for p in g.resource_patterns)
                )
                return await self._finish(
                    query, lang, at,
                    _Proposal(Verdict.DENY, Msg.DENY_RESOURCE_OUT_OF_SCOPE,
                              reason=DenyReason.RESOURCE_OUT_OF_SCOPE,
                              fields={"scope": ", ".join(
                                  safe_fragment(p) for p in scope_patterns) or "-"}),
                    None,
                )

        # --- stage 4: taint -------------------------------------------------------
        # The prompt-injection defence. This is why A.R.S can read email at all.
        if query.tainted and (sensitive(cap) or exfiltrates(cap)):
            taint_fields = self._taint_fields(query, lang)
            if cap.risk is Risk.CRITICAL:
                return await self._finish(
                    query, lang, at,
                    _Proposal(Verdict.DENY, Msg.DENY_TAINTED_CRITICAL,
                              reason=DenyReason.TAINTED_TURN, fields=taint_fields),
                    grant,
                )
            # Outbound-only capabilities get a prompt that shows the user the literal
            # text about to leave the machine — that is the whole control here, since
            # the guard is not permitted to inspect content itself.
            msg = (Msg.ASK_TAINTED_EGRESS
                   if exfiltrates(cap) and not sensitive(cap) else Msg.ASK_TAINTED)
            proposal.lower_to(Verdict.ASK, msg,
                              fields=taint_fields, priority=_TAINT_PRIORITY)

        # --- stage 5: confirm policy ----------------------------------------------
        if grant is not None:
            if grant.confirm is ConfirmPolicy.EVERY_USE:
                proposal.lower_to(Verdict.ASK, Msg.ASK_EVERY_USE)
            elif grant.confirm is ConfirmPolicy.ONCE_PER_SESSION:
                if self.consents.has(query.session_id, cap, grant.id):
                    proposal.lower_to(Verdict.ALLOW, Msg.ALLOW_SESSION_CONFIRMED)
                else:
                    proposal.lower_to(Verdict.ASK, Msg.ASK_ONCE_PER_SESSION)
            elif not silent_use_permitted(cap):
                # confirm=NEVER on a capability above LOW risk. Not obeyed.
                proposal.lower_to(Verdict.ASK, Msg.ASK_EVERY_USE)

        # --- stage 6: rate limit --------------------------------------------------
        if proposal.verdict is not Verdict.DENY:
            ok, scope, limit = self.limiter.check_and_consume(
                query.session_id, query.turn_id, cap)
            if not ok:
                proposal = _Proposal(
                    Verdict.DENY,
                    Msg.DENY_RATE_LIMITED_TURN if scope == "turn"
                    else Msg.DENY_RATE_LIMITED_SESSION,
                    reason=DenyReason.RATE_LIMITED,
                    fields={"limit": limit},
                )

        return await self._finish(query, lang, at, proposal, grant)

    # ------------------------------------------------------------------ outcome

    async def record_outcome(self, query: GuardQuery, decision: GuardDecision,
                             outcome: str, user_confirmed: bool | None = None) -> None:
        """Close the loop on a decision: append the outcome line to the audit log.

        If the user approved an ASK for a ``ONCE_PER_SESSION`` grant, this is also where
        that approval is remembered for the session - but **never from a tainted turn**.
        A "yes" that an attacker's text talked the user into must not become a standing
        permission for the rest of the conversation; it buys exactly one action.
        """
        record_id = self._pending.pop(self._decision_key(query, decision), None)
        if record_id is not None:
            await self.audit.complete(record_id, outcome=outcome,
                                      user_confirmed=user_confirmed)

        if (user_confirmed
                and decision.verdict is Verdict.ASK
                and decision.matched_grant_id is not None
                and not query.tainted):
            grant = await self.grants.get(decision.matched_grant_id)
            if grant is not None and grant.confirm is ConfirmPolicy.ONCE_PER_SESSION:
                self.consents.confirm(query.session_id, query.capability, grant.id)

    # ------------------------------------------------------------------ consent UI

    def consent_request(self, query: GuardQuery, decision: GuardDecision) -> GrantRequest:
        """Build the ``GrantRequest`` for an ASK, with a guard-composed reason.

        ``GrantRequest.reason`` is documented as quoted verbatim to the user, which makes
        it an injection surface if anything but the guard writes it. So it is exactly the
        decision's own explanation.
        """
        return GrantRequest(
            capability=query.capability,
            resource_patterns=(query.resource,) if query.resource else ("*",),
            reason=decision.explanation,
            suggested_confirm=ConfirmPolicy.EVERY_USE,
        )

    # ------------------------------------------------------------------ session end

    def end_session(self, session_id: str) -> None:
        self.consents.forget_session(session_id)
        self.limiter.forget_session(session_id)
        self._session_language.pop(session_id, None)

    async def revoke(self, grant_id: str, *, reason: str | None = None) -> bool:
        """Revoke a grant and drop any session consent riding on it.

        Revocation that leaves a live session approval behind has not revoked anything
        until the user restarts the app.
        """
        ok = await self.grants.revoke(grant_id, reason=reason)
        if ok:
            self.consents.forget_grant(grant_id)
        return ok

    # ------------------------------------------------------------------ internals

    def _taint_fields(self, query: GuardQuery, lang: Language) -> dict[str, Any]:
        """Name the taint source, using only guard-owned words plus a sanitised origin.

        ``uri`` and ``label`` are attacker-influenced - an email display name, a page
        title - so they go through ``safe_fragment`` before being spoken. The sentence
        around them is a template; only the identifier inside the parentheses varies.
        """
        prov = self._primary_taint(query)
        if prov is None:
            return {"source": unknown_source(lang), "origin": unknown_origin(lang)}
        origin = safe_fragment(prov.uri) or safe_fragment(prov.label) or unknown_origin(lang)
        return {"source": source_name(prov.source, lang), "origin": origin}

    @staticmethod
    def _primary_taint(query: GuardQuery) -> Provenance | None:
        external = [p for p in query.taint_sources if p.trust is TrustLevel.EXTERNAL]
        pool = external or list(query.taint_sources)
        if not pool:
            return None
        return max(pool, key=lambda p: p.fetched_at_ms)

    def _explain(self, query: GuardQuery, lang: Language, proposal: _Proposal) -> str:
        fields: dict[str, Any] = dict(proposal.fields)
        fields.setdefault("capability", capability_name(query.capability, lang))
        fields.setdefault("resource", safe_fragment(query.resource) or any_resource(lang))
        # The whole point of this line: a tainted turn does not get to write the words
        # the user is about to approve.
        fields.setdefault("summary", "" if query.tainted else safe_fragment(query.summary))
        return render(proposal.key, lang, **fields)

    @staticmethod
    def _decision_key(query: GuardQuery, decision: GuardDecision) -> tuple[Any, ...]:
        return (query.session_id, query.turn_id, str(query.capability), query.resource,
                decision.decided_at_ms, str(decision.verdict))

    async def _finish(self, query: GuardQuery, lang: Language, at: int,
                      proposal: _Proposal,
                      grant: CapabilityGrant | None) -> GuardDecision:
        """Compose the explanation, write the audit line, return the decision.

        The audit line is written here - inside ``evaluate``, before the caller has been
        told it may proceed. There is no path through this class that returns an ALLOW
        that is not already on disk and fsync'd.
        """
        decision = GuardDecision(
            verdict=proposal.verdict,
            capability=query.capability,
            reason=proposal.reason,
            explanation=self._explain(query, lang, proposal),
            matched_grant_id=grant.id if grant is not None else None,
            requires_confirmation=proposal.verdict is Verdict.ASK,
            decided_at_ms=at,
        )
        record = await self.audit.begin(AuditRecord(
            at_ms=at,
            session_id=query.session_id,
            turn_id=query.turn_id,
            capability=query.capability,
            resource=query.resource,
            verdict=decision.verdict,
            reason=decision.reason,
            risk=query.capability.risk,
            tainted=query.tainted,
            grant_id=decision.matched_grant_id,
        ))
        self._pending[self._decision_key(query, decision)] = record.id
        while len(self._pending) > MAX_PENDING_DECISIONS:
            self._pending.popitem(last=False)
        return decision
