"""The test that matters most for this store, per CLAUDE.md non-negotiable #7
("deletion must actually delete... tested, not assumed") and the task brief that says it
applies with more force to health data than anything else in this system. Insert, then
prove — by scanning the raw sqlite file directly, including the write-ahead log, not by
trusting this store's own query methods — that a forgotten reading's value is gone.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from ars_medical import MedicalConfig, SqliteMedicalStore
from ars_protocol import VitalKind, VitalReading

# A value unlikely to appear anywhere else in the database (default ranges, ledger rows,
# etc.) so a match is unambiguous evidence of the deleted reading, not a coincidence.
MARKER_VALUE = 137.0


def _raw_connect(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(db_path)


def _every_table(conn: sqlite3.Connection) -> list[str]:
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return [row[0] for row in cursor.fetchall()]


def _table_contains(conn: sqlite3.Connection, table: str, needle: str) -> bool:
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 — table name from sqlite_master only
    return any(needle in repr(row) for row in rows)


async def test_forget_leaves_no_trace_in_the_database_file(tmp_path: Path):
    config = MedicalConfig(data_dir=tmp_path)
    store = await SqliteMedicalStore.open(config)

    reading = VitalReading(kind=VitalKind.HEART_RATE, value=MARKER_VALUE)
    await store.record(reading)

    # Sanity: it really is there before we delete it.
    assert await store.get(reading.id) is not None
    assert (await store.latest(VitalKind.HEART_RATE)).value == MARKER_VALUE

    deleted_count = await store.forget(reading_id=reading.id)
    assert deleted_count == 1

    assert await store.get(reading.id) is None
    assert await store.latest(VitalKind.HEART_RATE) is None

    await store.close()

    # The real assertion: scan the actual sqlite file — every table, and the id itself —
    # not through ars_medical's own query methods, which could be lying to us.
    raw_conn = _raw_connect(config.db_path)
    try:
        tables = _every_table(raw_conn)
        assert "vital_readings" in tables
        offending = [
            t
            for t in tables
            if _table_contains(raw_conn, t, str(MARKER_VALUE))
            or _table_contains(raw_conn, t, reading.id)
        ]
        assert offending == [], f"deleted reading still present in: {offending}"
    finally:
        raw_conn.close()


async def test_forget_checkpoints_the_wal_so_deleted_bytes_do_not_survive_on_disk(tmp_path: Path):
    """`secure_delete=ON` scrubs the main database file, but the original INSERT frame
    survives in `<db>-wal` until the WAL is checkpointed. A forensic scan (or just
    `strings medical.db-wal`) looking for a deleted blood-pressure value would find it
    there even after `forget()` reports success, unless the checkpoint actually runs."""
    config = MedicalConfig(data_dir=tmp_path)
    store = await SqliteMedicalStore.open(config)

    reading = VitalReading(kind=VitalKind.HEART_RATE, value=MARKER_VALUE)
    await store.record(reading)
    await store.forget(reading_id=reading.id)

    wal_path = config.db_path.with_name(config.db_path.name + "-wal")
    if wal_path.exists():
        wal_bytes = wal_path.read_bytes()
        assert str(MARKER_VALUE).encode() not in wal_bytes, (
            "deleted value survives in the WAL — forget() did not really delete it on disk"
        )

    await store.close()


async def test_forgetting_a_correction_restores_the_reading_it_replaced(store):
    """Forgetting a correction (the user decides the correction itself was a mistake)
    must not leave the original reading pointing at a ghost id — it becomes current
    again, exactly like `ars_memory.store.forget()`."""
    old = await store.record(VitalReading(kind=VitalKind.HEART_RATE, value=180.0))
    new = await store.record(VitalReading(kind=VitalKind.HEART_RATE, value=72.0))
    await store.record(old.model_copy(update={"superseded_by": new.id}))

    await store.forget(reading_id=new.id)

    assert (await store.latest(VitalKind.HEART_RATE)).id == old.id


async def test_forget_requires_at_least_one_filter(store):
    """An unfiltered `forget()` on a health-data store is exactly the footgun this
    signature refuses to offer — a caller must say what it means to delete."""
    with pytest.raises(ValueError):
        await store.forget()


async def test_forget_by_kind_removes_only_that_kind(store):
    hr = await store.record(VitalReading(kind=VitalKind.HEART_RATE, value=70.0))
    spo2 = await store.record(VitalReading(kind=VitalKind.SPO2, value=98.0))

    deleted = await store.forget(kind=VitalKind.HEART_RATE)

    assert deleted == 1
    assert await store.get(hr.id) is None
    assert await store.get(spo2.id) is not None
