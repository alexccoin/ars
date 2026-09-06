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

from dataclasses import dataclass

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
    # Accept the obvious spellings. The package is called sentence-transformers, so
    # that is what people write in a .env file, and failing on it produces a stack trace
    # at startup rather than an assistant.
    backend = config.embedding_backend.strip().lower().replace("-", "_").rstrip("s")
    if backend == "hash":
        return HashEmbeddingBackend()
    if backend == "sentence_transformer":
        from .embeddings.sentence_transformer_backend import (
            SentenceTransformerEmbeddingBackend,
        )

        return SentenceTransformerEmbeddingBackend(config.sentence_transformer_model)
    raise ValueError(f"unknown embedding backend: {config.embedding_backend!r}")


@dataclass(frozen=True, slots=True)
class Scored:
    """A recall hit with the numbers that produced it."""

    rank: float
    """The blended score that ordered results: vector + keyword + recency."""
    cosine: float | None
    """Raw vector similarity, or None if this row was found by keyword search alone."""
    record: MemoryRecord


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
        self._last_scores: tuple[Scored, ...] = ()

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
        store = cls(
            conn=conn,
            vector_index=vector_index,
            embedding_backend=backend,
            config=config,
            vec_backend_active=vec_backend_active,
        )
        await store._reconcile_embedding_model()
        return store

    async def close(self) -> None:
        await self._conn.close()

    # --------------------------------------------------------------------------- meta

    async def meta_set(self, key: str, value: str) -> None:
        """Store a fact *about* the store rather than in it.

        Deliberately not recall-able: a catalogue entry is bookkeeping, and putting it in
        `memory_records` would make it retrievable as if it were something the user said.
        """
        await self._conn.execute(
            "INSERT INTO memory_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self._conn.commit()

    async def meta_get(self, key: str) -> str | None:
        cursor = await self._conn.execute(
            "SELECT value FROM memory_meta WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row["value"] if row else None

    async def meta_delete(self, key: str) -> bool:
        cursor = await self._conn.execute("DELETE FROM memory_meta WHERE key = ?", (key,))
        await self._conn.commit()
        return bool(cursor.rowcount)

    async def meta_items(self, prefix: str) -> list[tuple[str, str]]:
        cursor = await self._conn.execute(
            "SELECT key, value FROM memory_meta WHERE key LIKE ? ESCAPE '\\' ORDER BY key",
            (f"{_escape_like(prefix)}%",),
        )
        return [(row["key"], row["value"]) for row in await cursor.fetchall()]

    async def records_by_uri_prefix(self, prefix: str) -> tuple[MemoryRecord, ...]:
        """Every live record whose provenance uri starts with `prefix`.

        The catalogue of learned documents is a view over the chunks that actually exist,
        and this is what makes that possible without a second source of truth.
        """
        rows = await self._fetch_rows_where(
            "provenance_uri LIKE ? ESCAPE '\\' AND superseded_by IS NULL",
            (f"{_escape_like(prefix)}%",),
        )
        return tuple(self._row_to_record(row) for row in rows)

    async def exists(self, record_id: str) -> bool:
        """Whether a record is still stored. Used by callers that keep their own index
        into memory and have to notice when something underneath them was deleted."""
        cursor = await self._conn.execute(
            "SELECT 1 FROM memory_records WHERE id = ?", (record_id,)
        )
        return (await cursor.fetchone()) is not None

    # --------------------------------------------------------------- embedding identity

    async def _reconcile_embedding_model(self) -> int:
        """Re-embed everything if the model that built this index is not the one loaded.

        Vectors from two different models share a coordinate space the way two people's
        handwriting shares an alphabet — the numbers line up and mean nothing. A mixed
        index does not fail, it just returns wrong passages confidently, which is the
        worst failure mode this system has. So the model's identity is stored next to the
        index and checked on every open. Cost is one embedding pass over the records the
        user has, once, on the run after the model changes.

        Returns the number of records re-embedded.
        """
        identity = self._embedding.identity
        recorded = await self.meta_get("embedding_identity")
        if recorded == identity:
            return 0

        rows = list(await (await self._conn.execute(
            "SELECT rowid, text FROM memory_records"
        )).fetchall())
        if rows and recorded is not None:
            logger.warning(
                "embedding model changed (%s -> %s); re-embedding %d records",
                recorded, identity, len(rows),
            )
        if rows:
            vectors = await self._embedding.embed_passages([r["text"] for r in rows])
            for row_, vector in zip(rows, vectors, strict=True):
                await self._vector_index.upsert(row_["rowid"], vector)
        await self.meta_set("embedding_identity", identity)
        return len(rows)

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
        vector = await self._embedding.embed_passage(record.text)
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
            qvec = await self._embedding.embed_query(query)
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

            scored: list[tuple[float, float | None, aiosqlite.Row]] = []
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
                scored.append((score, vector_map.get(row["rowid"]), row))

            scored.sort(key=lambda triple: triple[0], reverse=True)
            top = scored[:limit]
            counter("ars_memory.recall.returned").add(len(top))
            self._last_scores = tuple(
                Scored(rank=rank, cosine=cos, record=self._row_to_record(row))
                for rank, cos, row in top
            )
            return tuple(hit.record for hit in self._last_scores)

    async def recall_scored(
        self, query: str, *, limit: int = 8, language: Language | None = None
    ) -> tuple[Scored, ...]:
        """`recall`, but keeping the numbers.

        A caller that has to decide *whether the match is good enough* — the tiered brain
        deciding if a document answers the question without waking the model — needs the
        score, and `MemoryRecord` is frozen so it cannot be smuggled onto the record.
        Both the blended rank and the raw cosine come back: the rank is what ordered them,
        the cosine is what a confidence threshold should be calibrated against, because
        the blend also contains keyword and recency terms that say nothing about meaning.
        """
        await self.recall(query, limit=limit, language=language)
        return self._last_scores

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
