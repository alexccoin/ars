"""Per-session guard state: what the user already said yes to, and how often we've asked.

Both of the things in this file are deliberately **in memory only**. A session consent
("yes, read my calendar, for this conversation") that survived a restart would be a
standing grant the user never created, hiding in a cache. When the process dies, the
answer to "may I" goes back to "ask them".
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Final

from ars_protocol import Capability

MAX_TRACKED_SESSIONS: Final = 256
"""Bound on both structures. A voice assistant on a laptop has a handful of live
sessions; anything past this is a leak or an attack, and evicting the oldest is the
safe direction to fail (evicting a consent means we ask again)."""

MAX_TRACKED_TURNS_PER_SESSION: Final = 64


class SessionConsentLedger:
    """Remembers ``ConfirmPolicy.ONCE_PER_SESSION`` approvals for the life of a session.

    Keyed by (session, capability, grant id). The grant id is part of the key on purpose:
    if the user revokes a grant and creates a new one, the old session approval does not
    silently carry over to the new grant.
    """

    def __init__(self, max_sessions: int = MAX_TRACKED_SESSIONS) -> None:
        self._max = max_sessions
        self._by_session: OrderedDict[str, set[tuple[Capability, str]]] = OrderedDict()

    def confirm(self, session_id: str, capability: Capability, grant_id: str) -> None:
        entries = self._by_session.get(session_id)
        if entries is None:
            entries = set()
            self._by_session[session_id] = entries
            while len(self._by_session) > self._max:
                self._by_session.popitem(last=False)
        self._by_session.move_to_end(session_id)
        entries.add((capability, grant_id))

    def has(self, session_id: str, capability: Capability, grant_id: str) -> bool:
        entries = self._by_session.get(session_id)
        return entries is not None and (capability, grant_id) in entries

    def forget_session(self, session_id: str) -> None:
        """Called when a session ends, and by the "forget what I approved" control."""
        self._by_session.pop(session_id, None)

    def forget_grant(self, grant_id: str) -> None:
        """Called on revocation: an approval for a revoked grant must not linger."""
        for entries in self._by_session.values():
            for key in [k for k in entries if k[1] == grant_id]:
                entries.discard(key)

    def clear(self) -> None:
        self._by_session.clear()


@dataclass(frozen=True)
class RateLimit:
    per_turn: int
    per_session: int


@dataclass
class RateLimitPolicy:
    """How often one capability may be exercised, per turn and per session.

    The failure mode this exists for is not a malicious user - it is a model in a loop.
    An agent that retries ``email.send`` forty times because a tool returned something it
    did not expect has sent forty emails. The per-turn limit for CRITICAL capabilities is
    1: doing an irreversible, externally-visible thing twice in one turn is always a bug.
    """

    default: RateLimit = RateLimit(per_turn=4, per_session=64)
    effectful: RateLimit = RateLimit(per_turn=2, per_session=24)
    critical: RateLimit = RateLimit(per_turn=1, per_session=8)
    overrides: dict[Capability, RateLimit] = field(default_factory=dict)

    def for_capability(self, capability: Capability) -> RateLimit:
        from ars_protocol import Risk

        if capability in self.overrides:
            return self.overrides[capability]
        if capability.risk is Risk.CRITICAL:
            return self.critical
        if capability.is_effectful:
            return self.effectful
        return self.default


class RateLimiter:
    """Counts capability uses per turn and per session.

    Only calls that were about to be allowed or asked about are counted - a denial does
    not consume the user's budget, because a denied call did nothing.
    """

    def __init__(self, policy: RateLimitPolicy | None = None,
                 max_sessions: int = MAX_TRACKED_SESSIONS) -> None:
        self.policy = policy or RateLimitPolicy()
        self._max = max_sessions
        self._session_counts: OrderedDict[str, dict[Capability, int]] = OrderedDict()
        self._turn_counts: OrderedDict[str, OrderedDict[str, dict[Capability, int]]] = (
            OrderedDict()
        )

    def _session_bucket(self, session_id: str) -> dict[Capability, int]:
        bucket = self._session_counts.get(session_id)
        if bucket is None:
            bucket = {}
            self._session_counts[session_id] = bucket
            while len(self._session_counts) > self._max:
                self._session_counts.popitem(last=False)
        self._session_counts.move_to_end(session_id)
        return bucket

    def _turn_bucket(self, session_id: str, turn_id: str) -> dict[Capability, int]:
        turns = self._turn_counts.get(session_id)
        if turns is None:
            turns = OrderedDict()
            self._turn_counts[session_id] = turns
            while len(self._turn_counts) > self._max:
                self._turn_counts.popitem(last=False)
        self._turn_counts.move_to_end(session_id)
        bucket = turns.get(turn_id)
        if bucket is None:
            bucket = {}
            turns[turn_id] = bucket
            while len(turns) > MAX_TRACKED_TURNS_PER_SESSION:
                turns.popitem(last=False)
        turns.move_to_end(turn_id)
        return bucket

    def check(self, session_id: str, turn_id: str,
              capability: Capability) -> tuple[bool, str, int]:
        """Would one more use fit? Returns (ok, scope, limit). Does not consume."""
        limit = self.policy.for_capability(capability)
        turn_used = self._turn_bucket(session_id, turn_id).get(capability, 0)
        if turn_used >= limit.per_turn:
            return False, "turn", limit.per_turn
        session_used = self._session_bucket(session_id).get(capability, 0)
        if session_used >= limit.per_session:
            return False, "session", limit.per_session
        return True, "", 0

    def consume(self, session_id: str, turn_id: str, capability: Capability) -> None:
        turn = self._turn_bucket(session_id, turn_id)
        turn[capability] = turn.get(capability, 0) + 1
        sess = self._session_bucket(session_id)
        sess[capability] = sess.get(capability, 0) + 1

    def check_and_consume(self, session_id: str, turn_id: str,
                          capability: Capability) -> tuple[bool, str, int]:
        ok, scope, limit = self.check(session_id, turn_id, capability)
        if ok:
            self.consume(session_id, turn_id, capability)
        return ok, scope, limit

    def forget_session(self, session_id: str) -> None:
        self._session_counts.pop(session_id, None)
        self._turn_counts.pop(session_id, None)

    def clear(self) -> None:
        self._session_counts.clear()
        self._turn_counts.clear()
