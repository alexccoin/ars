"""SqliteGrantStore — the record of what the user actually allowed.

Append-only, on purpose
-----------------------
There is no ``UPDATE`` and no ``DELETE`` in this module, and the schema has triggers that
abort if anyone adds one. A grant is a statement the user made at a point in time; a
revocation is a second statement they made later. Both are facts, and the user is
entitled to see both. If revoking a grant rewrote the row, then "what did I allow last
March, and when did I take it back" becomes unanswerable, and the consent record becomes
whatever the current state happens to be - which is exactly the shape of record that lets
a system quietly re-interpret what it was allowed to do.

Changing a grant is therefore: insert a revocation row for the old one, insert a new
grant row. Two lines of history, not one mutated row.

Hot path
--------
The architecture doc allocates the guard **0 ms** and forbids I/O on the decision path.
So the store keeps the full grant set in memory and serves :meth:`find` and
:meth:`active_grants` from there; SQLite is touched on open, on grant, and on revoke.
The cache is authoritative for this process, which means the store assumes a single
writer process. A second process that writes grants will not be seen until
:meth:`reload` is called - see ``security/threat-models/guard.md``.
"""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Final

import aiosqlite
from ars_core.interfaces import GrantStore
from ars_protocol import Capability, CapabilityGrant, ConfirmPolicy, GrantSource, now_ms

from .migrations import apply_migrations

__all__ = ["GrantConflictError", "SqliteGrantStore"]

_STRICTNESS: Final[dict[ConfirmPolicy, int]] = {
    ConfirmPolicy.NEVER: 0,
    ConfirmPolicy.ONCE_PER_SESSION: 1,
    ConfirmPolicy.EVERY_USE: 2,
}


class GrantConflictError(RuntimeError):
    """Raised when a caller tries to write a grant id that already exists.

    Append-only means an id is written once. Silently accepting a second write for the
    same id would be an in-place update wearing a disguise.
    """


def _row_to_grant(row: Sequence[Any]) -> CapabilityGrant:
    (gid, capability, patterns_json, confirm, granted_at_ms,
     expires_at_ms, source, note, revoked_at_ms) = row
    return CapabilityGrant(
        id=gid,
        capability=Capability(capability),
        resource_patterns=tuple(json.loads(patterns_json)),
        confirm=ConfirmPolicy(confirm),
        granted_at_ms=granted_at_ms,
        expires_at_ms=expires_at_ms,
        source=GrantSource(source),
        note=note,
        revoked_at_ms=revoked_at_ms,
    )


def specificity(grant: CapabilityGrant, resource: str | None) -> int:
    """How narrow this grant is for this resource: literal characters in the pattern.

    ``("*",)`` scores 0. ``("from:bank.ro",)`` scores 12. Used to pick between two grants
    that both cover the same call, so that a deliberate narrow grant beats a leftover
    broad one.
    """
    best = 0
    for pattern in grant.resource_patterns:
        if resource is not None and not fnmatch.fnmatchcase(resource, pattern):
            continue
        best = max(best, len(pattern.replace("*", "").replace("?", "")))
    return best


class SqliteGrantStore(GrantStore):
    """`GrantStore` over SQLite, append-only, with an in-memory hot path."""

    def __init__(self, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "grants.db"
        self._db: aiosqlite.Connection | None = None
        self._cache: list[CapabilityGrant] = []

    # ------------------------------------------------------------------ lifecycle

    async def open(self) -> SqliteGrantStore:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.db_path.parent.chmod(0o700)
        except OSError:  # pragma: no cover - shared parent dir
            pass
        existed = self.db_path.exists()
        self._db = await aiosqlite.connect(self.db_path)
        if not existed:
            try:
                self.db_path.chmod(0o600)
            except OSError:  # pragma: no cover
                pass
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA synchronous=FULL")
        await apply_migrations(self._db, "grants")
        await self.reload()
        return self

    async def aclose(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> SqliteGrantStore:
        return await self.open()

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                        tb: TracebackType | None) -> None:
        await self.aclose()

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("SqliteGrantStore used before open()")
        return self._db

    async def reload(self) -> None:
        """Re-read every grant from disk into the in-memory cache."""
        async with self._conn.execute(
            "SELECT id, capability, resource_patterns, confirm, granted_at_ms, "
            "       expires_at_ms, source, note, revoked_at_ms "
            "FROM grants_effective ORDER BY granted_at_ms ASC, id ASC"
        ) as cur:
            rows = await cur.fetchall()
        self._cache = [_row_to_grant(r) for r in rows]

    # ------------------------------------------------------------------ GrantStore

    async def active_grants(self) -> tuple[CapabilityGrant, ...]:
        at = now_ms()
        return tuple(g for g in self._cache if g.is_active(at))

    async def grant(self, grant: CapabilityGrant) -> CapabilityGrant:
        """Append a new grant. Never overwrites; a repeated id is an error."""
        db = self._conn
        async with db.execute("SELECT 1 FROM grants WHERE id = ?", (grant.id,)) as cur:
            if await cur.fetchone() is not None:
                raise GrantConflictError(
                    f"grant {grant.id} already exists; grants are append-only - "
                    "revoke it and insert a new one"
                )
        await db.execute(
            "INSERT INTO grants (id, capability, resource_patterns, confirm, "
            "  granted_at_ms, expires_at_ms, source, note, written_at_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                grant.id,
                str(grant.capability),
                json.dumps(list(grant.resource_patterns)),
                str(grant.confirm),
                grant.granted_at_ms,
                grant.expires_at_ms,
                str(grant.source),
                grant.note,
                now_ms(),
            ),
        )
        # A grant handed to us already revoked is preserved faithfully: the revocation is
        # a row, like every other revocation.
        if grant.revoked_at_ms is not None:
            await db.execute(
                "INSERT INTO grant_revocations (grant_id, revoked_at_ms, reason, written_at_ms) "
                "VALUES (?, ?, ?, ?)",
                (grant.id, grant.revoked_at_ms, "created_revoked", now_ms()),
            )
        await db.commit()
        await self.reload()
        stored = await self.get(grant.id)
        assert stored is not None
        return stored

    async def revoke(self, grant_id: str, *, reason: str | None = None,
                     at_ms: int | None = None) -> bool:
        """Append a revocation row. Returns False if there is no such grant.

        Revoking an already-revoked grant returns True and appends nothing new - the
        effective revocation time is the earliest one, so a duplicate cannot move it
        later.
        """
        db = self._conn
        async with db.execute("SELECT 1 FROM grants WHERE id = ?", (grant_id,)) as cur:
            if await cur.fetchone() is None:
                return False
        existing = await self.get(grant_id)
        if existing is not None and existing.revoked_at_ms is not None:
            return True
        await db.execute(
            "INSERT INTO grant_revocations (grant_id, revoked_at_ms, reason, written_at_ms) "
            "VALUES (?, ?, ?, ?)",
            (grant_id, at_ms if at_ms is not None else now_ms(), reason, now_ms()),
        )
        await db.commit()
        await self.reload()
        return True

    async def find(self, capability: Capability, resource: str | None) -> CapabilityGrant | None:
        """The active grant that covers this call, or None.

        When several grants cover the same call, the **most restrictive** one wins:
        ranked by confirm policy strictness, then by how narrowly its pattern matches,
        then by recency. Two overlapping permissions must never combine into more access
        than either of them gave on its own.
        """
        at = now_ms()
        matches = [g for g in self._cache
                   if g.capability is capability and g.covers(capability, resource, at)]
        if not matches:
            return None
        return max(matches, key=lambda g: (_STRICTNESS[g.confirm],
                                           specificity(g, resource),
                                           g.granted_at_ms))

    # ------------------------------------------------------------------ history

    async def get(self, grant_id: str) -> CapabilityGrant | None:
        for g in self._cache:
            if g.id == grant_id:
                return g
        return None

    async def all_grants(self) -> tuple[CapabilityGrant, ...]:
        """Every grant ever written, including expired and revoked ones.

        This is the "show me the history of what I allowed" query, and it is the reason
        the table is append-only.
        """
        return tuple(self._cache)

    async def grants_for(self, capability: Capability,
                         *, include_inactive: bool = True) -> tuple[CapabilityGrant, ...]:
        at = now_ms()
        return tuple(g for g in self._cache
                     if g.capability is capability and (include_inactive or g.is_active(at)))

    async def revocations(self, grant_id: str) -> tuple[dict[str, Any], ...]:
        async with self._conn.execute(
            "SELECT revoked_at_ms, reason, written_at_ms FROM grant_revocations "
            "WHERE grant_id = ? ORDER BY revoked_at_ms ASC",
            (grant_id,),
        ) as cur:
            rows = await cur.fetchall()
        return tuple({"revoked_at_ms": r[0], "reason": r[1], "written_at_ms": r[2]} for r in rows)

    async def revoke_all(self, capabilities: Iterable[Capability] | None = None,
                         *, reason: str | None = None) -> int:
        """Panic button: revoke every active grant, or every one for given capabilities."""
        wanted = set(capabilities) if capabilities is not None else None
        at = now_ms()
        count = 0
        for g in list(self._cache):
            if not g.is_active(at):
                continue
            if wanted is not None and g.capability not in wanted:
                continue
            if await self.revoke(g.id, reason=reason, at_ms=at):
                count += 1
        return count
