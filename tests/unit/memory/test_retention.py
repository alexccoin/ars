"""Retention sweeper vs security/policies/retention.md:
  * valid_until_ms always wins, regardless of sensitivity.
  * SENSITIVE has a hard 90-day cap even with no valid_until_ms and no supersession.
  * PERSONAL/PUBLIC current (non-superseded) facts with no valid_until_ms are durable.
  * superseded records are purged after their (shorter) historical window.
"""

from __future__ import annotations

from ars_memory.retention import RetentionSweeper
from ars_protocol import (
    Language,
    MemoryKind,
    MemoryRecord,
    Provenance,
    Sensitivity,
    SourceKind,
    TrustLevel,
)

DAY_MS = 86_400_000


def _record(text: str, sensitivity: Sensitivity, created_at_ms: int, **kwargs) -> MemoryRecord:
    return MemoryRecord(
        kind=MemoryKind.FACT,
        text=text,
        language=Language.EN,
        sensitivity=sensitivity,
        provenance=Provenance(source=SourceKind.MICROPHONE, trust=TrustLevel.USER),
        created_at_ms=created_at_ms,
        **kwargs,
    )


async def test_expired_valid_until_is_swept_regardless_of_sensitivity(store, config):
    now = 10_000_000_000
    rec = await store.remember(
        _record("on the train", Sensitivity.PUBLIC, now - DAY_MS, valid_until_ms=now - 1)
    )

    report = await RetentionSweeper(store, config).sweep(at_ms=now)

    assert report.expired_valid_until == 1
    assert report.total_deleted == 1
    assert await store.debug_rowid_for(rec.id) is None


async def test_sensitive_record_hard_capped_even_without_expiry(store, config):
    now = 10_000_000_000
    cap_days = config.retention_sensitive_cap_days
    old_enough = now - int((cap_days + 1) * DAY_MS)
    rec = await store.remember(
        _record("I take medication X", Sensitivity.SENSITIVE, old_enough)
    )

    report = await RetentionSweeper(store, config).sweep(at_ms=now)

    assert report.sensitivity_cap == 1
    assert await store.debug_rowid_for(rec.id) is None


async def test_sensitive_record_under_cap_survives(store, config):
    now = 10_000_000_000
    cap_days = config.retention_sensitive_cap_days
    recent = now - int((cap_days - 1) * DAY_MS)
    rec = await store.remember(
        _record("I take medication X", Sensitivity.SENSITIVE, recent)
    )

    report = await RetentionSweeper(store, config).sweep(at_ms=now)

    assert report.sensitivity_cap == 0
    assert await store.debug_rowid_for(rec.id) is not None


async def test_personal_current_record_with_no_expiry_is_durable(store, config):
    now = 10_000_000_000
    very_old = now - 10 * 365 * DAY_MS
    rec = await store.remember(_record("I live in Cluj", Sensitivity.PERSONAL, very_old))

    report = await RetentionSweeper(store, config).sweep(at_ms=now)

    assert report.total_deleted == 0
    assert await store.debug_rowid_for(rec.id) is not None


async def test_superseded_personal_record_purged_after_historical_window(store, config):
    now = 10_000_000_000
    window_days = config.retention_personal_superseded_days
    old_enough = now - int((window_days + 1) * DAY_MS)

    old = await store.remember(_record("I live in Cluj", Sensitivity.PERSONAL, old_enough))
    new = await store.remember(_record("I moved to Bucharest", Sensitivity.PERSONAL, now))
    await store.remember(old.model_copy(update={"superseded_by": new.id}))

    report = await RetentionSweeper(store, config).sweep(at_ms=now)

    assert report.superseded_purged == 1
    assert await store.debug_rowid_for(old.id) is None
    assert await store.debug_rowid_for(new.id) is not None


async def test_sweep_is_logged_without_content(store, config, tmp_path):
    import sqlite3

    now = 10_000_000_000
    await store.remember(
        _record("secret detail", Sensitivity.PUBLIC, now - DAY_MS, valid_until_ms=now - 1)
    )
    await RetentionSweeper(store, config).sweep(at_ms=now)

    conn = sqlite3.connect(config.db_path)
    try:
        rows = conn.execute("SELECT * FROM retention_sweeps").fetchall()
        assert len(rows) == 1
        assert "secret" not in repr(rows[0])
    finally:
        conn.close()
