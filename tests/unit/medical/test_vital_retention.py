"""`RetentionSweeper` vs `security/policies/retention-medical.md`. Read that file
first — the policy is deliberately asymmetric versus `ars_memory`'s: live readings are
kept indefinitely by default (the longitudinal series is the entire value of this
store), superseded (corrected) readings are purged after a bounded window, and a
caller can still opt a user into an automatic cap on live readings via
`retention_current_days`.
"""

from __future__ import annotations

from ars_medical import MedicalConfig, RetentionSweeper
from ars_protocol import VitalKind, VitalReading

DAY_MS = 86_400_000


async def test_a_live_reading_survives_indefinitely_by_default(store, config):
    """The core policy claim: unlike ars_memory's SENSITIVE cap, a vital reading gets
    no automatic hard cap just for being old and un-superseded — deleting it would
    destroy the longitudinal trend this store exists to answer questions about."""
    now = 10_000_000_000
    ancient = now - 10 * 365 * DAY_MS  # ten years old
    reading = await store.record(
        VitalReading(kind=VitalKind.HEART_RATE, value=70.0, measured_at_ms=ancient)
    )

    report = await RetentionSweeper(store, config).sweep(at_ms=now)

    assert report.total_deleted == 0
    assert await store.get(reading.id) is not None


async def test_a_superseded_reading_is_purged_after_the_historical_window(store, config):
    window_days = config.retention_superseded_days
    now = 10_000_000_000
    old_enough = now - int((window_days + 1) * DAY_MS)

    old = await store.record(
        VitalReading(kind=VitalKind.HEART_RATE, value=180.0, measured_at_ms=old_enough)
    )
    new = await store.record(
        VitalReading(kind=VitalKind.HEART_RATE, value=72.0, measured_at_ms=now)
    )
    await store.record(old.model_copy(update={"superseded_by": new.id}))

    report = await RetentionSweeper(store, config).sweep(at_ms=now)

    assert report.superseded_purged == 1
    assert await store.get(old.id) is None
    assert await store.get(new.id) is not None, "the current reading must never be touched"


async def test_a_superseded_reading_under_the_window_survives(store, config):
    window_days = config.retention_superseded_days
    now = 10_000_000_000
    recent = now - int((window_days - 1) * DAY_MS)

    old = await store.record(
        VitalReading(kind=VitalKind.HEART_RATE, value=180.0, measured_at_ms=recent)
    )
    new = await store.record(
        VitalReading(kind=VitalKind.HEART_RATE, value=72.0, measured_at_ms=now)
    )
    await store.record(old.model_copy(update={"superseded_by": new.id}))

    report = await RetentionSweeper(store, config).sweep(at_ms=now)

    assert report.superseded_purged == 0
    assert await store.get(old.id) is not None


async def test_a_caller_can_opt_a_live_reading_into_an_automatic_cap(tmp_path):
    """`retention_current_days=None` is the default, not the only option — a caller
    with a real reason (a user preference, a jurisdiction's requirement) can set an
    explicit cap and the sweeper honours it exactly like ars_memory's does."""
    from ars_medical import SqliteMedicalStore

    config = MedicalConfig(data_dir=tmp_path, retention_current_days=365)
    capped_store = await SqliteMedicalStore.open(config)
    try:
        now = 10_000_000_000
        old_enough = now - 400 * DAY_MS
        reading = await capped_store.record(
            VitalReading(kind=VitalKind.HEART_RATE, value=70.0, measured_at_ms=old_enough)
        )

        report = await RetentionSweeper(capped_store, config).sweep(at_ms=now)

        assert report.current_capped == 1
        assert await capped_store.get(reading.id) is None
    finally:
        await capped_store.close()


async def test_forgetting_a_superseded_reading_heals_the_pointer_on_the_current_one(store, config):
    """The sweep purges through the same `forget()` path a user-initiated deletion
    uses, so it inherits the same healing behaviour: the surviving reading must never
    be left pointing at a ghost id if something upstream of it were ever purged."""
    window_days = config.retention_superseded_days
    now = 10_000_000_000
    old_enough = now - int((window_days + 1) * DAY_MS)

    old = await store.record(
        VitalReading(kind=VitalKind.HEART_RATE, value=180.0, measured_at_ms=old_enough)
    )
    new = await store.record(
        VitalReading(kind=VitalKind.HEART_RATE, value=72.0, measured_at_ms=now)
    )
    await store.record(old.model_copy(update={"superseded_by": new.id}))

    await RetentionSweeper(store, config).sweep(at_ms=now)

    assert (await store.latest(VitalKind.HEART_RATE)).id == new.id


async def test_sweep_is_logged_without_a_reading_id_or_value(store, config, tmp_path):
    import sqlite3

    now = 10_000_000_000
    window_days = config.retention_superseded_days
    old_enough = now - int((window_days + 1) * DAY_MS)
    old = await store.record(
        VitalReading(kind=VitalKind.HEART_RATE, value=137.0, measured_at_ms=old_enough)
    )
    new = await store.record(
        VitalReading(kind=VitalKind.HEART_RATE, value=70.0, measured_at_ms=now)
    )
    await store.record(old.model_copy(update={"superseded_by": new.id}))

    await RetentionSweeper(store, config).sweep(at_ms=now)

    conn = sqlite3.connect(config.db_path)
    try:
        rows = conn.execute("SELECT * FROM vital_retention_sweeps").fetchall()
        assert len(rows) == 1
        row_repr = repr(rows[0])
        assert "137.0" not in row_repr
        assert old.id not in row_repr
    finally:
        conn.close()
