"""`SqliteMemoryStore` — the only implementation of `ars_core.interfaces.MemoryStore`.

Backend result, stated plainly: **sqlite-vec installed and loaded cleanly** on this
repo's arm64 `.venv` (`sqlite_vec==0.1.9`, verified both `sqlite3.Connection.
enable_load_extension` and an actual `vec0` virtual table with cosine distance). This
store therefore uses `SqliteVecIndex` by default. The `NumpyBruteForceIndex` fallback
(`ars_memory.vector_index.numpy_index`) is still fully wired behind the same
`VectorIndex` interface and is used automatically — no config flag needed — on any
machine where the extension fails to load (`ars_memory.db.sqlite_vec_available` probes
this on every connection, since SQLite extension loading is per-connection, not
persisted in the file).

Ranking formula: see `ars_memory.ranking` module docstring.

Key design decisions, so the "why" is next to the code that depends on it:

  * **`remember` is an upsert keyed by `MemoryRecord.id`, and it enforces
    correction-by-supersession** (requirement 4): a record already in the database is
    immutable except for one allowed transition, `superseded_by` going from `NULL` to a
    value, exactly once. Anything else — changing `text`, re-superseding, or
    un-superseding — raises. Callers correct a fact by creating a *new* `MemoryRecord`
    (via `remember`) and then calling `remember` again on a copy of the *old* record
    (`old.model_copy(update={"superseded_by": new.id})`) — `Model` is frozen, so this is
    the only way to express the transition, which is the point: the old row is never
    silently overwritten.

  * **`forget` is the only path that deletes**, and everything (record, FTS row, vector)
    goes through it — the retention sweeper (`ars_memory.retention`) calls it too,
    rather than duplicating deletion logic.

  * **`pending_observations` only returns observations that currently satisfy
    `Observation.may_ask`** (requirement 6) — evidence >= 2, confidence >= 0.6, not
    already asked. A caller that ignores `.may_ask` entirely still cannot make A.R.S
    nag, because the store never hands back an ineligible observation as "pending".
    `observe()` additionally refuses to persist a transition into "asked" for an
    observation that does not (yet) satisfy the gate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

import aiosqlite
from ars_core.interfaces import MemoryStore
from ars_protocol import (
    BehaviourDomain,
    Language,
    MemoryKind,
    MemoryRecord,
    Observation,
    ObservationStatus,
    Preference,
    Provenance,
    Sensitivity,
    SourceKind,
    TrustLevel,
    new_id,
    now_ms,
)
from ars_telemetry import aspan, counter

from . import db, ranking
from .config import MemoryConfig
from .embeddings.base import EmbeddingBackend
from .embeddings.hash_backend import HashEmbeddingBackend
from .vector_index.base import VectorIndex
from .vector_index.numpy_index import NumpyBruteForceIndex

logger = logging.getLogger("ars_memory.store")

_RECORD_COLUMNS = (
    "rowid, id, kind, text, language, sensitivity, provenance_source, provenance_trust, "
    "provenance_uri, provenance_fetched_at_ms, provenance_label, created_at_ms, "
    "valid_until_ms, superseded_by"
)


def _build_embedding_backend(config: MemoryConfig) -> EmbeddingBackend:
    if config.embedding_backend == "hash":
        return HashEmbeddingBackend()
    if config.embedding_backend == "sentence_transformer":
        from .embeddings.sentence_transformer_backend import (
            SentenceTransformerEmbeddingBackend,
        )

        return SentenceTransformerEmbeddingBackend(config.sentence_transformer_model)
    raise ValueError(f"unknown embedding backend: {config.embedding_backend!r}")


class SqliteMemoryStore(MemoryStore):
    def __init__(
        self,
        *,
        conn: aiosqlite.Connection,
        vector_index: VectorIndex,
        embedding_backend: EmbeddingBackend,
        config: MemoryConfig,
        vec_backend_active: bool,
    ) -> None:
        self._conn = conn
        self._vector_index = vector_index
        self._embedding = embedding_backend
        self._config = config
        self._vec_backend_active = vec_backend_active
        self._weights = ranking.RankWeights(
            vector=config.rank_weight_vector,
            keyword=config.rank_weight_keyword,
            recency=config.rank_weight_recency,
        )
        self._lock = asyncio.Lock()

    @property
    def vec_backend_active(self) -> bool:
        """True if sqlite-vec is doing the vector search; False means the numpy
        brute-force fallback is active. Exposed for tests and startup logging."""
        return self._vec_backend_active

    @classmethod
    async def open(
        cls,
        config: MemoryConfig,
        *,
        embedding_backend: EmbeddingBackend | None = None,
        migrations_dir: Path | None = None,
    ) -> SqliteMemoryStore:
        conn, vec_available = await db.open_connection(config.db_path)
        await db.migrate(conn, migrations_dir)
        vector_index: VectorIndex
        vec_backend_active = False
        if vec_available:
            try:
                from .vector_index.sqlite_vec_index import SqliteVecIndex

                vector_index = SqliteVecIndex(conn)
                vec_backend_active = True
            except ImportError:
                logger.warning(
                    "sqlite_vec probed available but import failed; using numpy fallback"
                )
                vector_index = NumpyBruteForceIndex(conn)
        else:
            vector_index = NumpyBruteForceIndex(conn)
        backend = embedding_backend or _build_embedding_backend(config)
        return cls(
            conn=conn,
            vector_index=vector_index,
            embedding_backend=backend,
            config=config,
            vec_backend_active=vec_backend_active,
        )

    async def close(self) -> None:
        await self._conn.close()

    # ------------------------------------------------------------- test/debug helpers

    async def debug_rowid_for(self, record_id: str) -> int | None:
        """Test/debug only: the SQLite rowid backing `record_id`, or `None` if it does
        not exist. Exists so deletion tests can check the vector index directly by
        rowid even after the record itself (and therefore the id -> rowid mapping) has
        been removed by `forget()`."""
        cursor = await self._conn.execute(
            "SELECT rowid FROM memory_records WHERE id = ?", (record_id,)
        )
        row = await cursor.fetchone()
        return row["rowid"] if row is not None else None

    async def vector_index_contains_rowid(self, rowid: int) -> bool:
        """Test/debug only."""
        return await self._vector_index.contains(rowid)

    # ---------------------------------------------------------------- remember / recall

    async def remember(self, record: MemoryRecord) -> MemoryRecord:
        async with aspan(
            "memory.remember", kind=record.kind.value, sensitivity=record.sensitivity.value
        ):
            async with self._lock:
                existing = await self._fetch_record_by_id(record.id)
                if existing is None:
                    await self._insert_record(record)
                    counter("ars_memory.remember.inserted").add(1, kind=record.kind.value)
                    return record
                self._validate_supersession_update(existing, record)
                if existing.superseded_by == record.superseded_by:
                    return existing
                await self._conn.execute(
                    "UPDATE memory_records SET superseded_by = ? WHERE id = ?",
                    (record.superseded_by, record.id),
                )
                await self._conn.commit()
                counter("ars_memory.remember.superseded").add(1)
                return record

    @staticmethod
    def _validate_supersession_update(existing: MemoryRecord, incoming: MemoryRecord) -> None:
        if existing.superseded_by is not None and incoming.superseded_by != existing.superseded_by:
            raise ValueError(
                f"memory record {existing.id} is already superseded by "
                f"{existing.superseded_by!r}; superseded_by is never rewritten"
            )
        unchanged = existing.model_copy(update={"superseded_by": incoming.superseded_by})
        if unchanged != incoming:
            raise ValueError(
                f"memory record {existing.id} already exists and only its "
                "superseded_by field may change; create a new record and supersede "
                "this one instead of overwriting it"
            )

    async def _insert_record(self, record: MemoryRecord) -> None:
        cursor = await self._conn.execute(
            """
            INSERT INTO memory_records (
                id, kind, text, language, sensitivity,
                provenance_source, provenance_trust, provenance_uri,
                provenance_fetched_at_ms, provenance_label,
                created_at_ms, valid_until_ms, superseded_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.kind.value,
                record.text,
                record.language.value,
                record.sensitivity.value,
                record.provenance.source.value,
                record.provenance.trust.value,
                record.provenance.uri,
                record.provenance.fetched_at_ms,
                record.provenance.label,
                record.created_at_ms,
                record.valid_until_ms,
                record.superseded_by,
            ),
        )
        rowid = cursor.lastrowid
        assert rowid is not None
        vector = await self._embedding.embed(record.text)
        await self._vector_index.upsert(rowid, vector)
        await self._conn.execute(
            "INSERT INTO memory_fts (rowid, text) VALUES (?, ?)", (rowid, record.text)
        )
        await self._conn.commit()

    async def recall(
        self, query: str, *, limit: int = 8, language: Language | None = None
    ) -> tuple[MemoryRecord, ...]:
        lang_attr = language.value if language else None
        async with aspan("memory.recall", limit=limit, language=lang_attr):
            qvec = await self._embedding.embed(query)
            vector_hits = await self._vector_index.search(
                qvec, limit=self._config.rank_vector_candidates
            )
            keyword_hits = await self._keyword_search(
                query, limit=self._config.rank_keyword_candidates
            )
            candidate_rowids = {rowid for rowid, _ in vector_hits} | {
                rowid for rowid, _ in keyword_hits
            }
            if not candidate_rowids:
                return ()

            rows = await self._fetch_rows_by_rowid(candidate_rowids)
            vector_map = dict(vector_hits)
            keyword_map = dict(keyword_hits)
            now = now_ms()

            scored: list[tuple[float, aiosqlite.Row]] = []
            for row in rows:
                if row["superseded_by"] is not None:
                    continue
                if row["valid_until_ms"] is not None and row["valid_until_ms"] <= now:
                    continue
                if language is not None and row["language"] != language.value:
                    continue
                score = ranking.combine(
                    vector_sim=vector_map.get(row["rowid"]),
                    bm25_raw=keyword_map.get(row["rowid"]),
                    age_ms=now - row["created_at_ms"],
                    weights=self._weights,
                    half_life_days=self._config.rank_recency_half_life_days,
                )
                scored.append((score, row))

            scored.sort(key=lambda pair: pair[0], reverse=True)
            top = scored[:limit]
            counter("ars_memory.recall.returned").add(len(top))
            return tuple(self._row_to_record(row) for _, row in top)

    async def _keyword_search(self, query: str, *, limit: int) -> list[tuple[int, float]]:
        match_expr = _fts_match_expr(query)
        if match_expr is None:
            return []
        try:
            cursor = await self._conn.execute(
                "SELECT rowid, bm25(memory_fts) AS rank FROM memory_fts "
                "WHERE memory_fts MATCH ? ORDER BY rank LIMIT ?",
                (match_expr, limit),
            )
            rows = await cursor.fetchall()
        except aiosqlite.Error as exc:
            logger.warning("FTS query failed for sanitized expr %r: %s", match_expr, exc)
            return []
        return [(row["rowid"], row["rank"]) for row in rows]

    # ------------------------------------------------------------------------- forget

    async def forget(self, *, record_id: str | None = None, matching: str | None = None) -> int:
        if record_id is None and matching is None:
            raise ValueError("forget() requires record_id or matching")
        async with aspan("memory.forget", by="id" if record_id else "matching"):
            async with self._lock:
                if record_id is not None:
                    rows = await self._fetch_rows_where("id = ?", (record_id,))
                else:
                    assert matching is not None
                    pattern = f"%{_escape_like(matching.lower())}%"
                    rows = await self._fetch_rows_where(
                        "LOWER(text) LIKE ? ESCAPE '\\'", (pattern,)
                    )
                if not rows:
                    return 0

                rowids = [row["rowid"] for row in rows]
                ids = [row["id"] for row in rows]

                # Heal dangling pointers: a record that was superseded *by* one of the
                # ids we are about to forget becomes current again, rather than
                # pointing at a ghost.
                for id_ in ids:
                    await self._conn.execute(
                        "UPDATE memory_records SET superseded_by = NULL WHERE superseded_by = ?",
                        (id_,),
                    )

                for rowid in rowids:
                    await self._vector_index.delete(rowid)
                    await self._conn.execute("DELETE FROM memory_fts WHERE rowid = ?", (rowid,))
                await self._conn.executemany(
                    "DELETE FROM memory_records WHERE rowid = ?", [(r,) for r in rowids]
                )
                await self._conn.commit()
                # secure_delete scrubs the main database, but the original INSERT
                # frames still sit in the write-ahead log until it is checkpointed —
                # deleted text survives in memory.db-wal, which is exactly where a
                # forensic scan looks. Truncate the WAL so the deletion is real on
                # disk. Off the hot path: forget() is rare and user-initiated.
                await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                counter("ars_memory.forget.deleted").add(len(rowids))
                return len(rowids)

    # -------------------------------------------------------------- behaviour learning

    async def observe(self, observation: Observation) -> Observation:
        async with aspan("memory.observe", domain=observation.domain.value):
            async with self._lock:
                existing = await self._fetch_observation(observation.id)
                if (
                    existing is not None
                    and existing.asked_at_ms is None
                    and observation.asked_at_ms is not None
                    and not existing.may_ask
                ):
                    raise PermissionError(
                        f"observation {observation.id} may not be marked asked: it does "
                        "not satisfy may_ask (needs >=2 evidence turns, confidence >= 0.6, "
                        "status PROPOSED, not already asked) — refusing so a buggy caller "
                        "cannot make A.R.S nag"
                    )
                await self._upsert_observation(observation)
                await self._conn.commit()
                return observation

    async def pending_observations(self) -> tuple[Observation, ...]:
        cursor = await self._conn.execute(
            "SELECT * FROM observations WHERE status = ? ORDER BY created_at_ms",
            (ObservationStatus.PROPOSED.value,),
        )
        rows = await cursor.fetchall()
        candidates = [self._row_to_observation(row) for row in rows]
        # Gate enforced here too, in addition to observe(): a buggy caller that never
        # checks `.may_ask` still only ever sees eligible observations.
        return tuple(o for o in candidates if o.may_ask)

    async def confirm_observation(self, observation_id: str, confirmed: bool) -> Preference | None:
        async with aspan("memory.confirm_observation", confirmed=confirmed):
            async with self._lock:
                existing = await self._fetch_observation(observation_id)
                if existing is None:
                    return None
                now = now_ms()
                if not confirmed:
                    rejected = existing.model_copy(
                        update={"status": ObservationStatus.REJECTED, "resolved_at_ms": now}
                    )
                    await self._upsert_observation(rejected)
                    await self._conn.commit()
                    return None

                if not (existing.may_ask or existing.asked_at_ms is not None):
                    raise PermissionError(
                        f"observation {observation_id} cannot be confirmed into a "
                        "preference: it has not passed the may_ask gate and was never "
                        "asked. A confirmed-in-silence preference is the failure mode "
                        "this store refuses to create."
                    )

                confirmed_obs = existing.model_copy(
                    update={"status": ObservationStatus.CONFIRMED, "resolved_at_ms": now}
                )
                await self._upsert_observation(confirmed_obs)
                preference = Preference(
                    domain=existing.domain,
                    statement=existing.statement,
                    from_observation_id=existing.id,
                )
                await self._conn.execute(
                    "INSERT INTO preferences (id, domain, statement, from_observation_id, "
                    "confirmed_at_ms, active) VALUES (?, ?, ?, ?, ?, 1)",
                    (
                        preference.id,
                        preference.domain.value,
                        preference.statement,
                        preference.from_observation_id,
                        preference.confirmed_at_ms,
                    ),
                )
                await self._conn.commit()
                counter("ars_memory.preference.confirmed").add(1, domain=preference.domain.value)
                return preference

    async def active_preferences(self) -> tuple[Preference, ...]:
        cursor = await self._conn.execute(
            "SELECT * FROM preferences WHERE active = 1 ORDER BY confirmed_at_ms"
        )
        rows = await cursor.fetchall()
        return tuple(self._row_to_preference(row) for row in rows)

    async def _upsert_observation(self, observation: Observation) -> None:
        await self._conn.execute(
            """
            INSERT INTO observations (
                id, domain, statement, confidence, evidence_turn_ids, status,
                created_at_ms, asked_at_ms, resolved_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                domain = excluded.domain,
                statement = excluded.statement,
                confidence = excluded.confidence,
                evidence_turn_ids = excluded.evidence_turn_ids,
                status = excluded.status,
                asked_at_ms = excluded.asked_at_ms,
                resolved_at_ms = excluded.resolved_at_ms
            """,
            (
                observation.id,
                observation.domain.value,
                observation.statement,
                observation.confidence,
                json.dumps(list(observation.evidence_turn_ids)),
                observation.status.value,
                observation.created_at_ms,
                observation.asked_at_ms,
                observation.resolved_at_ms,
            ),
        )

    async def _fetch_observation(self, observation_id: str) -> Observation | None:
        cursor = await self._conn.execute(
            "SELECT * FROM observations WHERE id = ?", (observation_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_observation(row) if row is not None else None

    @staticmethod
    def _row_to_observation(row: aiosqlite.Row) -> Observation:
        return Observation(
            id=row["id"],
            domain=BehaviourDomain(row["domain"]),
            statement=row["statement"],
            confidence=row["confidence"],
            evidence_turn_ids=tuple(json.loads(row["evidence_turn_ids"])),
            status=ObservationStatus(row["status"]),
            created_at_ms=row["created_at_ms"],
            asked_at_ms=row["asked_at_ms"],
            resolved_at_ms=row["resolved_at_ms"],
        )

    @staticmethod
    def _row_to_preference(row: aiosqlite.Row) -> Preference:
        return Preference(
            id=row["id"],
            domain=BehaviourDomain(row["domain"]),
            statement=row["statement"],
            from_observation_id=row["from_observation_id"],
            confirmed_at_ms=row["confirmed_at_ms"],
            active=bool(row["active"]),
        )

    # ------------------------------------------------------------------------- history

    async def history(self, record_id: str) -> tuple[MemoryRecord, ...]:
        """Not part of `MemoryStore` — an extra, tested convenience for callers that
        want the full correction chain of a fact (requirement 4: "history stays
        queryable"). Walks `superseded_by` in both directions from `record_id`."""
        seed = await self._fetch_record_by_id(record_id)
        if seed is None:
            return ()
        chain: dict[str, MemoryRecord] = {seed.id: seed}

        # forward: follow superseded_by
        cursor = seed
        while cursor.superseded_by is not None:
            nxt = await self._fetch_record_by_id(cursor.superseded_by)
            if nxt is None or nxt.id in chain:
                break
            chain[nxt.id] = nxt
            cursor = nxt

        # backward: find whoever points at any id currently in the chain
        changed = True
        while changed:
            changed = False
            for id_ in list(chain):
                predecessor_rows = await self._fetch_rows_where("superseded_by = ?", (id_,))
                for row in predecessor_rows:
                    if row["id"] not in chain:
                        chain[row["id"]] = self._row_to_record(row)
                        changed = True

        return tuple(sorted(chain.values(), key=lambda r: r.created_at_ms))

    # ------------------------------------------------------------------------ retention

    async def records_for_retention_scan(self) -> list[aiosqlite.Row]:
        """Used by `ars_memory.retention.RetentionSweeper`. Returns identifiers and
        metadata only — never `text` — because retention decisions never need to look
        at content, only at classification and timestamps."""
        cursor = await self._conn.execute(
            "SELECT id, sensitivity, created_at_ms, valid_until_ms, superseded_by "
            "FROM memory_records"
        )
        return await cursor.fetchall()

    async def record_retention_sweep(
        self,
        *,
        ran_at_ms: int,
        expired_valid_until_count: int,
        superseded_purged_count: int,
        sensitivity_cap_count: int,
        total_deleted: int,
    ) -> None:
        await self._conn.execute(
            "INSERT INTO retention_sweeps (id, ran_at_ms, expired_valid_until_count, "
            "superseded_purged_count, sensitivity_cap_count, total_deleted) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                new_id("rsw"),
                ran_at_ms,
                expired_valid_until_count,
                superseded_purged_count,
                sensitivity_cap_count,
                total_deleted,
            ),
        )
        await self._conn.commit()

    # ------------------------------------------------------------------------- helpers

    async def _fetch_record_by_id(self, record_id: str) -> MemoryRecord | None:
        rows = await self._fetch_rows_where("id = ?", (record_id,))
        return self._row_to_record(rows[0]) if rows else None

    async def _fetch_rows_where(self, where: str, params: tuple) -> list[aiosqlite.Row]:
        # `where` is always a literal string written at call sites in this module
        # (never derived from user input); every value goes through `params` as a
        # bound `?` placeholder. noqa: S608
        cursor = await self._conn.execute(
            f"SELECT {_RECORD_COLUMNS} FROM memory_records WHERE {where}",  # noqa: S608
            params,
        )
        return await cursor.fetchall()

    async def _fetch_rows_by_rowid(self, rowids: set[int]) -> list[aiosqlite.Row]:
        if not rowids:
            return []
        # `placeholders` is a fixed-shape string of `?` marks only — the actual rowid
        # values are always bound parameters, never interpolated.
        placeholders = ",".join("?" for _ in rowids)
        cursor = await self._conn.execute(
            f"SELECT {_RECORD_COLUMNS} FROM memory_records WHERE rowid IN ({placeholders})",  # noqa: S608
            tuple(rowids),
        )
        return await cursor.fetchall()

    @staticmethod
    def _row_to_record(row: aiosqlite.Row) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"],
            kind=MemoryKind(row["kind"]),
            text=row["text"],
            language=Language(row["language"]),
            sensitivity=Sensitivity(row["sensitivity"]),
            provenance=Provenance(
                source=SourceKind(row["provenance_source"]),
                trust=TrustLevel(row["provenance_trust"]),
                uri=row["provenance_uri"],
                fetched_at_ms=row["provenance_fetched_at_ms"],
                label=row["provenance_label"],
            ),
            created_at_ms=row["created_at_ms"],
            valid_until_ms=row["valid_until_ms"],
            superseded_by=row["superseded_by"],
        )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fts_match_expr(query: str) -> str | None:
    """Turns free text into a safe FTS5 MATCH expression: every alphanumeric token,
    individually double-quoted (so FTS5 syntax characters in the raw query can never
    produce a malformed query), OR'd together. `None` if the query has no tokens at all
    (pure punctuation/whitespace)."""
    words = re.findall(r"\w+", query, re.UNICODE)
    if not words:
        return None
    quoted = [f'"{w.replace(chr(34), chr(34) * 2)}"' for w in words]
    return " OR ".join(quoted)
