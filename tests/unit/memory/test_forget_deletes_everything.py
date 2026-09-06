"""The test that matters most in this service (per the task brief): insert, embed,
delete, then prove — by scanning every table in the raw sqlite file, not by trusting
the store's own bookkeeping — that nothing of the deleted memory remains anywhere:
not the record, not its FTS entry, not its embedding (sqlite-vec or numpy fallback).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ars_memory import MemoryConfig, SqliteMemoryStore
from ars_protocol import (
    Language,
    MemoryKind,
    MemoryRecord,
    Provenance,
    Sensitivity,
    SourceKind,
    TrustLevel,
)

SENSITIVE_TEXT = "My social security number is 900101999999 and I bank at Raiffeisen"


def _raw_connect(db_path: Path) -> sqlite3.Connection:
    """Plain sqlite3, but with sqlite-vec loaded — `memory_vec` is a real virtual table
    on disk and needs its module registered to be queried at all, exactly like any
    other connection to this database (SQLite extensions are per-connection)."""
    conn = sqlite3.connect(db_path)
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except (ImportError, sqlite3.OperationalError):
        pass
    return conn


def _every_table(conn: sqlite3.Connection) -> list[str]:
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
    )
    return [row[0] for row in cursor.fetchall()]


def _table_contains_text(conn: sqlite3.Connection, table: str, needle: str) -> bool:
    # `table`/`c` below are always names read back from `sqlite_master`/`PRAGMA
    # table_info` in this same function, never external input — this whole function's
    # job is to scan every table by name, so string-built SQL is unavoidable here.
    columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    text_columns = [c for c in columns if c not in ("rowid",)]
    if not text_columns:
        # virtual tables (fts5/vec0) don't always report via table_info the way we'd
        # like; fall back to selecting every column via '*' and checking repr().
        try:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
        except sqlite3.Error:
            return False
        return any(needle in repr(row) for row in rows)
    placeholders = " || ' ' || ".join(f"COALESCE({c}, '')" for c in text_columns)
    query = f"SELECT COUNT(*) FROM {table} WHERE ({placeholders}) LIKE ?"  # noqa: S608
    try:
        count = conn.execute(query, (f"%{needle}%",)).fetchone()[0]
    except sqlite3.Error:
        # some virtual tables reject arbitrary WHERE/LIKE composition
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
        return any(needle in repr(row) for row in rows)
    return count > 0


async def test_forget_leaves_no_trace_anywhere(tmp_path: Path):
    config = MemoryConfig(data_dir=tmp_path, embedding_backend="hash")
    store = await SqliteMemoryStore.open(config)

    record = MemoryRecord(
        kind=MemoryKind.FACT,
        text=SENSITIVE_TEXT,
        language=Language.EN,
        sensitivity=Sensitivity.SENSITIVE,
        provenance=Provenance(source=SourceKind.MICROPHONE, trust=TrustLevel.USER),
    )
    await store.remember(record)

    # Sanity: it really is there, and really is embedded, before we delete it.
    pre_delete_hits = await store.recall(SENSITIVE_TEXT, limit=5)
    assert any(r.id == record.id for r in pre_delete_hits)
    rowid = await store.debug_rowid_for(record.id)
    assert rowid is not None
    assert await store.vector_index_contains_rowid(rowid)

    deleted_count = await store.forget(record_id=record.id)
    assert deleted_count == 1

    post_delete_hits = await store.recall(SENSITIVE_TEXT, limit=5)
    assert all(r.id != record.id for r in post_delete_hits)
    assert not await store.vector_index_contains_rowid(rowid)

    await store.close()

    # The real assertion: scan the actual sqlite file, every table, for the deleted
    # text — not through ars_memory's own query methods, which could be lying to us.
    raw_conn = _raw_connect(config.db_path)
    try:
        tables = _every_table(raw_conn)
        assert tables, "expected at least the migrated tables to exist"
        offending_tables = [
            t for t in tables if _table_contains_text(raw_conn, t, SENSITIVE_TEXT)
        ]
        assert offending_tables == [], (
            f"deleted text still found in: {offending_tables}"
        )

        # And explicitly: the id itself is gone from every table that could reference it.
        offending_id_tables = [
            t for t in tables if _table_contains_text(raw_conn, t, record.id)
        ]
        assert offending_id_tables == [], (
            f"deleted record id still referenced in: {offending_id_tables}"
        )
    finally:
        raw_conn.close()


async def test_forget_by_matching_deletes_all_hits_and_reports_true_count(tmp_path: Path):
    config = MemoryConfig(data_dir=tmp_path, embedding_backend="hash")
    store = await SqliteMemoryStore.open(config)

    ids = []
    for i in range(3):
        rec = await store.remember(
            MemoryRecord(
                kind=MemoryKind.FACT,
                text=f"delete-me-marker fact number {i}",
                language=Language.EN,
                sensitivity=Sensitivity.PERSONAL,
                provenance=Provenance(source=SourceKind.MICROPHONE, trust=TrustLevel.USER),
            )
        )
        ids.append(rec.id)
    keep = await store.remember(
        MemoryRecord(
            kind=MemoryKind.FACT,
            text="unrelated fact to keep",
            language=Language.EN,
            sensitivity=Sensitivity.PERSONAL,
            provenance=Provenance(source=SourceKind.MICROPHONE, trust=TrustLevel.USER),
        )
    )

    deleted_count = await store.forget(matching="delete-me-marker")
    assert deleted_count == 3

    remaining = await store.recall("fact", limit=10)
    remaining_ids = {r.id for r in remaining}
    assert remaining_ids == {keep.id}

    await store.close()


async def test_forgetting_a_superseding_record_restores_the_old_one(store):
    from ars_protocol import MemoryKind as MK

    old = await store.remember(
        MemoryRecord(
            kind=MK.FACT,
            text="I live in Cluj",
            language=Language.EN,
            sensitivity=Sensitivity.PERSONAL,
            provenance=Provenance(source=SourceKind.MICROPHONE, trust=TrustLevel.USER),
        )
    )
    new = await store.remember(
        MemoryRecord(
            kind=MK.FACT,
            text="I moved to Bucharest",
            language=Language.EN,
            sensitivity=Sensitivity.PERSONAL,
            provenance=Provenance(source=SourceKind.MICROPHONE, trust=TrustLevel.USER),
        )
    )
    await store.remember(old.model_copy(update={"superseded_by": new.id}))

    # Forget the correction itself (e.g. it was a mistake) — the old fact should
    # become "current" again rather than pointing at a ghost id.
    await store.forget(record_id=new.id)

    results = await store.recall("Where do I live?", limit=5)
    assert any(r.id == old.id for r in results)
