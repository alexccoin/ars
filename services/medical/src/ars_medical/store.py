"""`SqliteMedicalStore` — vital readings, reference ranges, and findings.

Modelled directly on `ars_memory.store.SqliteMemoryStore`; read that module's docstring
first, the design decisions below are the same ones with "reading" in place of "record"
except where health data demands something stricter.

Key decisions, so the "why" is next to the code that depends on it:

  * **`record` is an upsert keyed by `VitalReading.id`, and enforces correction-by-
    supersession**, exactly like `SqliteMemoryStore.remember`: a reading already in the
    database is immutable except for one allowed transition, `superseded_by` going from
    `NULL` to a value, exactly once. Callers correct a reading by creating a *new*
    `VitalReading` and then re-`record`-ing a copy of the old one with `superseded_by`
    set (`Model` is frozen, so this is the only way to express the transition). The old
    row is never overwritten — "my systolic was 180 last Tuesday" and "it was recorded
    as 180 last Tuesday and corrected" are different facts.

  * **Implausible values are rejected, never stored — alarming ones are stored exactly
    as measured.** `VitalReading.is_plausible` (backed by `ars_protocol.health.
    PLAUSIBLE`) is the only filter `record`/`record_many` apply. Nothing here rounds,
    clamps, or otherwise adjusts a value that passes it.

  * **Identical `(kind, value, measured_at_ms)` is deduplicated**, enforced as a real
    UNIQUE index (`0009_vital_readings`), not just a Python-side check — a device
    resending its buffer must not be able to create a second row for the same
    measurement even under concurrent writers.

  * **`forget` is the only path that deletes**, and it checkpoints the WAL — see
    `ars_medical.db.wal_checkpoint_truncate` — for the same reason `ars_memory` does:
    `secure_delete=ON` scrubs the main database file, but the original INSERT frame
    survives in `medical.db-wal` until checkpointed, and "the user asked me to delete
    it" has to mean the bytes are gone, not just unreachable through this store's own
    queries. Non-negotiable #7 applies with more force here than anywhere else in this
    system.

  * **`findings` never says more than "this number is outside that named range."** See
    `RangeFinding`'s docstring in `packages/protocol/src/ars_protocol/health.py` — there
    is deliberately no type for a diagnosis, an assessment or a recommendation anywhere
    in this module, and none is synthesised here either.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import aiosqlite
from ars_protocol import (
    PLAUSIBLE,
    DeviceKind,
    RangeFinding,
    ReadingSource,
    ReferenceRange,
    Sensitivity,
    VitalKind,
    VitalReading,
    new_id,
    now_ms,
)
from ars_telemetry import aspan, counter

from . import db
from .config import MedicalConfig
from .ranges import DEFAULT_REFERENCE_RANGES

logger = logging.getLogger("ars_medical.store")


class ImplausibleReadingError(ValueError):
    """Raised by `record`/collected by `record_many` when a value falls outside
    `ars_protocol.health.PLAUSIBLE` for its kind — a device error (a slipped cuff, a
    cold finger on a pulse oximeter), not a rare-but-real measurement. Never stored.

    The message deliberately omits the measured value itself — SENSITIVE content that
    should not end up verbatim in a log line just because a caller's generic exception
    handler called `str()` on this. Only `reading.kind` (an identifier) and the
    plausible bounds (a published constant, not the user's data) go into the message.
    The value is still available on `.reading.value` for the one caller who already has
    it: whoever submitted this exact reading.
    """

    def __init__(self, reading: VitalReading) -> None:
        low, high = PLAUSIBLE[reading.kind]
        self.reading = reading
        self.low = low
        self.high = high
        super().__init__(
            f"{reading.kind.value} reading {reading.id} is outside the plausible "
            f"range [{low}, {high}] {reading.unit} for a working device and was not "
            "stored"
        )


@dataclass(frozen=True, slots=True)
class IngestResult:
    """What happened to a batch handed to `record_many` — a device's buffer is not
    all-or-nothing: one implausible entry must not block the readings around it, and a
    resend must not silently look identical to a fresh reading."""

    accepted: tuple[VitalReading, ...]
    """Newly inserted or newly superseded — this call changed the database for these."""
    deduplicated: tuple[VitalReading, ...]
    """Matched an existing `(kind, value, measured_at_ms)`. The reading returned is the
    one already stored (which may have an older `id` than the one that was submitted),
    not a new row."""
    rejected: tuple[tuple[VitalReading, str], ...]
    """`(attempted reading, reason)` — never stored. Never silently dropped either."""


class Trend(StrEnum):
    """A numeric description of a window, nothing more. Deliberately not "improving" /
    "worsening" — whether a rising number is good or bad depends on which vital it is
    (rising steps: good; rising systolic: not), which is exactly the kind of judgement
    `packages/protocol/src/ars_protocol/health.py` says this subsystem may not make."""

    RISING = "rising"
    FALLING = "falling"
    STABLE = "stable"
    UNKNOWN = "unknown"
    """Fewer than one reading in each half of the window — not enough to say."""


@dataclass(frozen=True, slots=True)
class Aggregate:
    """mean/min/max/count plus a trend, over one kind and one time window."""

    kind: VitalKind
    count: int
    mean: float | None
    minimum: float | None
    maximum: float | None
    trend: Trend


def _trend(
    first_count: int, first_mean: float | None, second_count: int, second_mean: float | None
) -> Trend:
    """Splits the window in two by time and compares the halves' means.

    The threshold is relative (2% of the first half's mean, floored to a tiny absolute
    epsilon so a mean of ~0 does not divide by nothing) rather than a fixed number,
    because "stable" has to mean something different for a heart rate in the 60s and a
    step count in the thousands. This is a description of the numbers, not a clinical
    judgement — see `Trend`'s docstring.
    """
    if first_count == 0 or second_count == 0 or first_mean is None or second_mean is None:
        return Trend.UNKNOWN
    threshold = max(abs(first_mean) * 0.02, 1e-9)
    delta = second_mean - first_mean
    if delta > threshold:
        return Trend.RISING
    if delta < -threshold:
        return Trend.FALLING
    return Trend.STABLE


class SqliteMedicalStore:
    def __init__(self, *, conn: aiosqlite.Connection, config: MedicalConfig) -> None:
        self._conn = conn
        self._config = config
        self._lock = asyncio.Lock()

    @classmethod
    async def open(
        cls, config: MedicalConfig, *, migrations_dir: Path | None = None
    ) -> SqliteMedicalStore:
        conn = await db.open_connection(config.db_path)
        await db.migrate(conn, migrations_dir)
        store = cls(conn=conn, config=config)
        if config.seed_default_ranges:
            await store._seed_default_ranges()
        return store

    async def close(self) -> None:
        await self._conn.close()

    # ---------------------------------------------------------------------------- ingest

    async def record(self, reading: VitalReading) -> VitalReading:
        """Insert, dedup, or apply a supersession — see module docstring. Raises
        `ImplausibleReadingError` rather than storing a device error."""
        async with aspan("medical.record", kind=reading.kind.value):
            status, result = await self._record_one(reading)
            counter(f"ars_medical.record.{status}").add(1, kind=reading.kind.value)
            return result

    async def record_many(self, readings: Sequence[VitalReading]) -> IngestResult:
        """Like `record`, but a bad entry in a device's buffer does not cost the rest of
        the buffer. See `IngestResult`."""
        accepted: list[VitalReading] = []
        deduplicated: list[VitalReading] = []
        rejected: list[tuple[VitalReading, str]] = []
        async with aspan("medical.record_many", count=len(readings)):
            for reading in readings:
                try:
                    status, result = await self._record_one(reading)
                except ImplausibleReadingError as exc:
                    rejected.append((reading, str(exc)))
                    continue
                if status == "deduplicated":
                    deduplicated.append(result)
                else:
                    accepted.append(result)
            counter("ars_medical.record_many.accepted").add(len(accepted))
            counter("ars_medical.record_many.deduplicated").add(len(deduplicated))
            counter("ars_medical.record_many.rejected").add(len(rejected))
        return IngestResult(
            accepted=tuple(accepted),
            deduplicated=tuple(deduplicated),
            rejected=tuple(rejected),
        )

    async def _record_one(self, reading: VitalReading) -> tuple[str, VitalReading]:
        if not reading.is_plausible:
            raise ImplausibleReadingError(reading)
        async with self._lock:
            existing = await self._fetch_by_id(reading.id)
            if existing is not None:
                self._validate_supersession_update(existing, reading)
                if existing.superseded_by == reading.superseded_by:
                    return "unchanged", existing
                await self._conn.execute(
                    "UPDATE vital_readings SET superseded_by = ? WHERE id = ?",
                    (reading.superseded_by, reading.id),
                )
                await self._conn.commit()
                return "superseded", reading
            duplicate = await self._fetch_duplicate(
                reading.kind, reading.value, reading.measured_at_ms
            )
            if duplicate is not None:
                return "deduplicated", duplicate
            await self._insert(reading)
            return "inserted", reading

    @staticmethod
    def _validate_supersession_update(existing: VitalReading, incoming: VitalReading) -> None:
        if existing.superseded_by is not None and incoming.superseded_by != existing.superseded_by:
            raise ValueError(
                f"vital reading {existing.id} is already superseded by "
                f"{existing.superseded_by!r}; superseded_by is never rewritten"
            )
        unchanged = existing.model_copy(update={"superseded_by": incoming.superseded_by})
        if unchanged != incoming:
            raise ValueError(
                f"vital reading {existing.id} already exists and only its "
                "superseded_by field may change; create a new reading and supersede "
                "this one instead of overwriting it"
            )

    async def _insert(self, reading: VitalReading) -> None:
        await self._conn.execute(
            """
            INSERT INTO vital_readings (
                id, kind, value, measured_at_ms, device_kind, device_name, device_id,
                note, sensitivity, superseded_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reading.id,
                reading.kind.value,
                reading.value,
                reading.measured_at_ms,
                reading.source.device_kind.value,
                reading.source.device_name,
                reading.source.device_id,
                reading.note,
                reading.sensitivity.value,
                reading.superseded_by,
            ),
        )
        await self._conn.commit()

    # ---------------------------------------------------------------------------- forget

    async def forget(
        self,
        *,
        reading_id: str | None = None,
        kind: VitalKind | None = None,
        before_ms: int | None = None,
    ) -> int:
        """Deletes readings matching every filter given (AND'd together) and returns how
        many rows were actually removed. At least one filter is required — an unfiltered
        `forget()` on a health-data store is exactly the footgun this signature refuses
        to offer; delete everything by passing `before_ms=now_ms()+1` explicitly if that
        is really what is meant.

        Checkpoints the WAL (`ars_medical.db.wal_checkpoint_truncate`) so the deletion is
        real on disk, not just unreachable through this store's own queries.
        """
        if reading_id is None and kind is None and before_ms is None:
            raise ValueError("forget() requires reading_id, kind, and/or before_ms")
        async with aspan("medical.forget", by="id" if reading_id else "filter"):
            async with self._lock:
                clauses: list[str] = []
                params: list[object] = []
                if reading_id is not None:
                    clauses.append("id = ?")
                    params.append(reading_id)
                if kind is not None:
                    clauses.append("kind = ?")
                    params.append(kind.value)
                if before_ms is not None:
                    clauses.append("measured_at_ms < ?")
                    params.append(before_ms)
                rows = await self._fetch_rows_where(" AND ".join(clauses), tuple(params))
                if not rows:
                    return 0
                ids = [row["id"] for row in rows]

                # Heal dangling pointers: a reading that was superseded *by* one of the
                # ids about to be forgotten becomes current again, rather than pointing
                # at a ghost — same healing ars_memory.store.forget() does.
                for id_ in ids:
                    await self._conn.execute(
                        "UPDATE vital_readings SET superseded_by = NULL WHERE superseded_by = ?",
                        (id_,),
                    )
                await self._conn.executemany(
                    "DELETE FROM vital_readings WHERE id = ?", [(i,) for i in ids]
                )
                await self._conn.commit()
                await db.wal_checkpoint_truncate(self._conn)
                counter("ars_medical.forget.deleted").add(len(ids))
                return len(ids)

    # --------------------------------------------------------------------------- reading

    async def get(self, reading_id: str) -> VitalReading | None:
        """One reading by id, superseded or not — a caller that asked for a specific id
        wants that id, not the store's opinion of what replaced it."""
        return await self._fetch_by_id(reading_id)

    async def history(self, reading_id: str) -> tuple[VitalReading, ...]:
        """The full correction chain for one reading, oldest first. Walks
        `superseded_by` in both directions, exactly like `SqliteMemoryStore.history`."""
        seed = await self._fetch_by_id(reading_id)
        if seed is None:
            return ()
        chain: dict[str, VitalReading] = {seed.id: seed}

        cursor = seed
        while cursor.superseded_by is not None:
            nxt = await self._fetch_by_id(cursor.superseded_by)
            if nxt is None or nxt.id in chain:
                break
            chain[nxt.id] = nxt
            cursor = nxt

        changed = True
        while changed:
            changed = False
            for id_ in list(chain):
                predecessors = await self._fetch_rows_where("superseded_by = ?", (id_,))
                for row in predecessors:
                    if row["id"] not in chain:
                        chain[row["id"]] = self._row_to_reading(row)
                        changed = True

        return tuple(sorted(chain.values(), key=lambda r: r.measured_at_ms))

    # --------------------------------------------------------------------------- queries

    async def latest(self, kind: VitalKind) -> VitalReading | None:
        """The most recent live (non-superseded) reading of one kind, or `None`."""
        async with aspan("medical.latest", kind=kind.value):
            cursor = await self._conn.execute(
                "SELECT * FROM vital_readings WHERE kind = ? AND superseded_by IS NULL "
                "ORDER BY measured_at_ms DESC, rowid DESC LIMIT 1",
                (kind.value,),
            )
            row = await cursor.fetchone()
            return self._row_to_reading(row) if row is not None else None

    async def latest_all(self) -> tuple[VitalReading, ...]:
        """The most recent live reading per kind that has ever been recorded — the
        answer to "how am I doing right now", one number per vital. One indexed query
        (a window function over `idx_vital_readings_kind_time`), not one query per
        `VitalKind`, so it stays CPU-tier fast regardless of how many kinds exist."""
        async with aspan("medical.latest_all"):
            cursor = await self._conn.execute(
                """
                SELECT * FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY kind ORDER BY measured_at_ms DESC, rowid DESC
                    ) AS rn
                    FROM vital_readings
                    WHERE superseded_by IS NULL
                ) WHERE rn = 1
                ORDER BY kind
                """
            )
            rows = await cursor.fetchall()
            return tuple(self._row_to_reading(r) for r in rows)

    async def series(
        self,
        kind: VitalKind,
        *,
        since_ms: int | None = None,
        until_ms: int | None = None,
        limit: int | None = None,
        include_superseded: bool = False,
    ) -> tuple[VitalReading, ...]:
        """Readings of one kind in `[since_ms, until_ms]`, oldest first. Bounded by the
        composite `(kind, measured_at_ms)` index — this is the query "what was my blood
        pressure last week" answers from, and it must never need a model to be fast."""
        async with aspan("medical.series", kind=kind.value, since=since_ms, until=until_ms):
            clauses = ["kind = ?"]
            params: list[object] = [kind.value]
            if not include_superseded:
                clauses.append("superseded_by IS NULL")
            if since_ms is not None:
                clauses.append("measured_at_ms >= ?")
                params.append(since_ms)
            if until_ms is not None:
                clauses.append("measured_at_ms <= ?")
                params.append(until_ms)
            query = (
                f"SELECT * FROM vital_readings WHERE {' AND '.join(clauses)} "  # noqa: S608
                "ORDER BY measured_at_ms ASC"
            )
            if limit is not None:
                query += " LIMIT ?"
                params.append(limit)
            cursor = await self._conn.execute(query, tuple(params))
            rows = await cursor.fetchall()
            return tuple(self._row_to_reading(r) for r in rows)

    async def aggregate(
        self, kind: VitalKind, *, since_ms: int, until_ms: int | None = None
    ) -> Aggregate:
        """mean/min/max/count/trend over `[since_ms, until_ms]`, computed entirely in
        SQL (`AVG`/`MIN`/`MAX`/`COUNT`, three indexed range scans) so the cost does not
        grow with how the caller chooses to read the result — no row is ever pulled into
        Python just to be averaged."""
        until_ms = until_ms if until_ms is not None else now_ms()
        async with aspan("medical.aggregate", kind=kind.value, since=since_ms, until=until_ms):
            count, mean, minimum, maximum = await self._agg_window(kind, since_ms, until_ms)
            if count == 0:
                return Aggregate(
                    kind=kind, count=0, mean=None, minimum=None, maximum=None, trend=Trend.UNKNOWN
                )
            midpoint = (since_ms + until_ms) // 2
            first_count, first_mean, _, _ = await self._agg_window(
                kind, since_ms, midpoint, until_inclusive=False
            )
            second_count, second_mean, _, _ = await self._agg_window(kind, midpoint, until_ms)
            trend = _trend(first_count, first_mean, second_count, second_mean)
            return Aggregate(
                kind=kind, count=count, mean=mean, minimum=minimum, maximum=maximum, trend=trend
            )

    async def _agg_window(
        self, kind: VitalKind, since_ms: int, until_ms: int, *, until_inclusive: bool = True
    ) -> tuple[int, float | None, float | None, float | None]:
        op = "<=" if until_inclusive else "<"
        # `op` is one of the two literal comparison operators above, never external
        # input; every value in the query goes through the bound params below.
        query = (
            "SELECT COUNT(*), AVG(value), MIN(value), MAX(value) FROM vital_readings "  # noqa: S608
            "WHERE kind = ? AND superseded_by IS NULL AND measured_at_ms >= ? "
            f"AND measured_at_ms {op} ?"
        )
        cursor = await self._conn.execute(query, (kind.value, since_ms, until_ms))
        row = await cursor.fetchone()
        assert row is not None, "COUNT(*) always returns exactly one row"
        count = row[0] or 0
        return count, row[1], row[2], row[3]

    # -------------------------------------------------------------------- range/findings

    async def _seed_default_ranges(self) -> None:
        for range_ in DEFAULT_REFERENCE_RANGES:
            await self.add_range(range_)

    async def add_range(self, range_: ReferenceRange) -> ReferenceRange:
        """Upsert keyed by `(kind, source)` — a source updating its own guidance
        overwrites its own row; two sources publishing a range for the same kind both
        get to keep one."""
        async with aspan("medical.add_range", kind=range_.kind.value):
            await self._conn.execute(
                """
                INSERT INTO reference_ranges (id, kind, low, high, source, note)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(kind, source) DO UPDATE SET
                    low = excluded.low, high = excluded.high, note = excluded.note
                """,
                (
                    new_id("refr"),
                    range_.kind.value,
                    range_.low,
                    range_.high,
                    range_.source,
                    range_.note,
                ),
            )
            await self._conn.commit()
            return range_

    async def ranges_for(self, kind: VitalKind) -> tuple[ReferenceRange, ...]:
        cursor = await self._conn.execute(
            "SELECT kind, low, high, source, note FROM reference_ranges WHERE kind = ? "
            "ORDER BY source",
            (kind.value,),
        )
        rows = await cursor.fetchall()
        return tuple(self._row_to_range(r) for r in rows)

    async def all_ranges(self) -> tuple[ReferenceRange, ...]:
        cursor = await self._conn.execute(
            "SELECT kind, low, high, source, note FROM reference_ranges ORDER BY kind, source"
        )
        rows = await cursor.fetchall()
        return tuple(self._row_to_range(r) for r in rows)

    async def findings(self, readings: Sequence[VitalReading]) -> tuple[RangeFinding, ...]:
        """A reading and the fact that it sits outside a named range — nothing more.
        Every currently-stored range for a reading's kind is checked, so a reading
        outside two different sources' ranges produces two findings, each with its own
        citation; the caller decides how to present that, this store never merges them
        into a single opinion. See `RangeFinding`'s docstring for why there is nothing
        stronger than this here."""
        async with aspan("medical.findings", count=len(readings)):
            ranges_by_kind: dict[VitalKind, tuple[ReferenceRange, ...]] = {}
            for kind in {r.kind for r in readings}:
                ranges_by_kind[kind] = await self.ranges_for(kind)

            results: list[RangeFinding] = []
            for reading in readings:
                for range_ in ranges_by_kind.get(reading.kind, ()):
                    if range_.contains(reading.value):
                        continue
                    below = range_.low is not None and reading.value < range_.low
                    results.append(
                        RangeFinding(
                            reading=reading, range=range_, direction="below" if below else "above"
                        )
                    )
            counter("ars_medical.findings.returned").add(len(results))
            return tuple(results)

    @staticmethod
    def _row_to_range(row: aiosqlite.Row) -> ReferenceRange:
        return ReferenceRange(
            kind=VitalKind(row["kind"]),
            low=row["low"],
            high=row["high"],
            source=row["source"],
            note=row["note"],
        )

    # ------------------------------------------------------------------------- helpers

    async def _fetch_by_id(self, reading_id: str) -> VitalReading | None:
        rows = await self._fetch_rows_where("id = ?", (reading_id,))
        return self._row_to_reading(rows[0]) if rows else None

    async def _fetch_duplicate(
        self, kind: VitalKind, value: float, measured_at_ms: int
    ) -> VitalReading | None:
        rows = await self._fetch_rows_where(
            "kind = ? AND value = ? AND measured_at_ms = ?", (kind.value, value, measured_at_ms)
        )
        return self._row_to_reading(rows[0]) if rows else None

    async def _fetch_rows_where(
        self, where: str, params: tuple[object, ...]
    ) -> list[aiosqlite.Row]:
        # `where` is always a literal string written at call sites in this module (never
        # derived from user input); every value goes through `params` as a bound `?`
        # placeholder. noqa: S608
        cursor = await self._conn.execute(
            f"SELECT * FROM vital_readings WHERE {where}", params  # noqa: S608
        )
        return list(await cursor.fetchall())

    @staticmethod
    def _row_to_reading(row: aiosqlite.Row) -> VitalReading:
        return VitalReading(
            id=row["id"],
            kind=VitalKind(row["kind"]),
            value=row["value"],
            measured_at_ms=row["measured_at_ms"],
            source=ReadingSource(
                device_kind=DeviceKind(row["device_kind"]),
                device_name=row["device_name"],
                device_id=row["device_id"],
            ),
            note=row["note"],
            sensitivity=Sensitivity(row["sensitivity"]),
            superseded_by=row["superseded_by"],
        )


__all__ = [
    "Aggregate",
    "ImplausibleReadingError",
    "IngestResult",
    "SqliteMedicalStore",
    "Trend",
]
