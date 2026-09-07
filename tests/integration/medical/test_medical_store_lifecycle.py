"""Integration tests for `services/medical`.

Two boundaries are exercised here, neither of which a pure unit test (which always
opens a fresh in-memory-equivalent tmp_path store) can catch:

1. **Process restart.** The store is closed and reopened against the same on-disk file,
   proving migrations are idempotent and nothing — a reading, a correction, a reference
   range — lives only in memory.

2. **The migrations-directory boundary with `services/memory`.** Both services point
   their own `migrate()` at a subtree of the shared repo-root `data/migrations/`
   (`ars_medical` at `data/migrations/medical/`, `ars_memory` at `data/migrations/`
   itself). See `ars_medical.db`'s module docstring for why that split exists: without
   it, opening either store would pull in the other's schema. This is exactly the kind
   of thing a unit test for one service alone would never notice — it only shows up
   when both stores are opened against real migration directories in the same process,
   which is what makes it an integration test rather than a unit one.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ars_medical import MedicalConfig, SqliteMedicalStore
from ars_protocol import (
    DeviceKind,
    ReadingSource,
    ReferenceRange,
    VitalKind,
    VitalReading,
)


async def test_full_lifecycle_survives_a_process_restart(tmp_path: Path):
    config = MedicalConfig(data_dir=tmp_path)

    # --- "process 1": ingest, correct, add a custom range ---
    store = await SqliteMedicalStore.open(config)
    old = await store.record(
        VitalReading(
            kind=VitalKind.BLOOD_PRESSURE_SYSTOLIC,
            value=180.0,
            source=ReadingSource(device_kind=DeviceKind.BLOOD_PRESSURE_MONITOR, device_id="cuff-1"),
        )
    )
    new = await store.record(
        VitalReading(
            kind=VitalKind.BLOOD_PRESSURE_SYSTOLIC,
            value=122.0,
            measured_at_ms=old.measured_at_ms + 1,
        )
    )
    await store.record(old.model_copy(update={"superseded_by": new.id}))
    await store.add_range(
        ReferenceRange(
            kind=VitalKind.BLOOD_PRESSURE_SYSTOLIC,
            low=90.0,
            high=120.0,
            source="Clinician's individual target for this patient",
        )
    )
    await store.close()

    # --- "process 2": reopen the same file, expect everything intact ---
    store2 = await SqliteMedicalStore.open(config)
    try:
        assert (await store2.latest(VitalKind.BLOOD_PRESSURE_SYSTOLIC)).id == new.id

        history = await store2.history(new.id)
        assert {r.id for r in history} == {old.id, new.id}
        assert next(r.value for r in history if r.id == old.id) == 180.0

        custom_ranges = [
            r for r in await store2.ranges_for(VitalKind.BLOOD_PRESSURE_SYSTOLIC)
            if r.source == "Clinician's individual target for this patient"
        ]
        assert len(custom_ranges) == 1

        findings = await store2.findings([new])
        # 122 is inside the default AHA "normal" range (90-120)? No: 122 > 120 -> a
        # finding against the default AHA range, and also outside the clinician's
        # individually-added 90-120 range -- both should survive the restart.
        assert len(findings) == 2
    finally:
        await store2.close()


async def test_forget_survives_a_restart(tmp_path: Path):
    config = MedicalConfig(data_dir=tmp_path)

    store = await SqliteMedicalStore.open(config)
    reading = await store.record(VitalReading(kind=VitalKind.WEIGHT, value=82.0))
    deleted = await store.forget(reading_id=reading.id)
    assert deleted == 1
    await store.close()

    store2 = await SqliteMedicalStore.open(config)
    try:
        assert await store2.get(reading.id) is None
    finally:
        await store2.close()


async def test_medical_and_memory_migrations_do_not_leak_into_each_others_database(tmp_path: Path):
    """The infrastructure claim in `ars_medical.db`'s module docstring, checked
    directly: opening a medical store must never create `memory_records` (or any other
    `ars_memory` table), and opening a memory store — pointed at the *same* repo-root
    `data/migrations/` tree — must never create `vital_readings` or `reference_ranges`.
    """
    from ars_memory import MemoryConfig, SqliteMemoryStore

    medical_config = MedicalConfig(data_dir=tmp_path / "medical")
    memory_config = MemoryConfig(data_dir=tmp_path / "memory", embedding_backend="hash")

    medical_store = await SqliteMedicalStore.open(medical_config)
    memory_store = await SqliteMemoryStore.open(memory_config)
    try:
        pass
    finally:
        await medical_store.close()
        await memory_store.close()

    medical_tables = _table_names(medical_config.db_path)
    memory_tables = _table_names(memory_config.db_path)

    assert "vital_readings" in medical_tables
    assert "reference_ranges" in medical_tables
    assert "memory_records" not in medical_tables, (
        "opening the medical store pulled ars_memory's schema into medical.db"
    )

    assert "memory_records" in memory_tables
    assert "vital_readings" not in memory_tables, (
        "opening the memory store pulled ars_medical's schema into memory.db"
    )
    assert "reference_ranges" not in memory_tables


def _table_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        return {row[0] for row in rows}
    finally:
        conn.close()
