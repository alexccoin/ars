"""Dedup on `(kind, value, measured_at_ms)` — a device that resends its buffer (a
common failure mode: a phone loses connectivity mid-sync and retries the whole window)
must not multiply a single real measurement into several phantom readings, which would
silently inflate `count` and skew `mean` for every query touching that window.
"""

from __future__ import annotations

from ars_protocol import VitalKind, VitalReading


async def test_resending_the_identical_measurement_does_not_create_a_second_row(store):
    """The device constructs a brand-new `VitalReading` each time it sends (a fresh
    `id`, per the protocol's `default_factory`) — dedup has to be on content, not on id,
    or a resend would never be recognised as one."""
    first = VitalReading(kind=VitalKind.HEART_RATE, value=68.0, measured_at_ms=1_700_000_000_000)
    resend = VitalReading(kind=VitalKind.HEART_RATE, value=68.0, measured_at_ms=1_700_000_000_000)
    assert first.id != resend.id, (
        "the two objects must genuinely be a fresh resend, not the same object"
    )

    stored_first = await store.record(first)
    stored_second = await store.record(resend)

    assert stored_first.id == stored_second.id, "the resend must resolve to the original row"

    series = await store.series(VitalKind.HEART_RATE)
    assert len(series) == 1, "a resend must not appear as a second reading in the series"


async def test_a_later_measurement_one_millisecond_apart_is_not_deduplicated(store):
    """The dedup key is exact — two genuinely different measurements a millisecond
    apart (a device that samples fast) must both survive, or a real second reading
    would be silently discarded as if it were a resend."""
    t = 1_700_000_000_000
    a = await store.record(VitalReading(kind=VitalKind.HEART_RATE, value=68.0, measured_at_ms=t))
    b = await store.record(
        VitalReading(kind=VitalKind.HEART_RATE, value=68.0, measured_at_ms=t + 1)
    )
    assert a.id != b.id
    assert len(await store.series(VitalKind.HEART_RATE)) == 2


async def test_record_many_deduplicates_within_a_single_buffer_and_reports_it(store):
    """A device's buffer upload is exactly the scenario dedup exists for: the same
    reading appearing twice in one payload (a bug on the device side, or a genuinely
    resent partial buffer) must come back as one accepted reading and one reported
    duplicate, never as two accepted rows."""
    t = 1_700_000_000_000
    reading = VitalReading(kind=VitalKind.SPO2, value=97.0, measured_at_ms=t)
    repeat = VitalReading(kind=VitalKind.SPO2, value=97.0, measured_at_ms=t)

    result = await store.record_many([reading, repeat])

    assert len(result.accepted) == 1
    assert len(result.deduplicated) == 1
    assert result.deduplicated[0].id == result.accepted[0].id
    assert len(await store.series(VitalKind.SPO2)) == 1
