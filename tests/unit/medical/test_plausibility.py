"""`PLAUSIBLE` (packages/protocol/src/ars_protocol/health.py) exists to catch device
error, not to second-guess a real, frightening number. A cuff that slipped off mid-
measurement reports 20/10; a genuinely hypertensive reading of 200/120 is alarming but
entirely something a cuff can measure correctly. Storing the first silently corrupts
every average computed afterwards; refusing to store the second would hide the one
reading a clinician most needs to see. This file is the test that the line is drawn in
the right place, and that nothing on the accepted side of it is ever adjusted.
"""

from __future__ import annotations

import pytest
from ars_medical import ImplausibleReadingError
from ars_protocol import VitalKind, VitalReading


async def test_a_slipped_cuff_reading_is_rejected_not_stored(store):
    """20/10 is what a cuff reports when it has slipped off the arm, not a blood
    pressure a living person has. If this were stored, every mean/min/max this store
    computes afterwards would be corrupted by a number that was never a measurement."""
    slipped = VitalReading(kind=VitalKind.BLOOD_PRESSURE_SYSTOLIC, value=20.0)
    with pytest.raises(ImplausibleReadingError):
        await store.record(slipped)

    assert await store.latest(VitalKind.BLOOD_PRESSURE_SYSTOLIC) is None


async def test_an_alarming_but_real_reading_is_stored_exactly_as_measured(store):
    """200 systolic is a hypertensive crisis, not a device error — PLAUSIBLE allows up
    to 260. This is the reading that matters most to keep, and it must come back with
    the exact value it was recorded with: no rounding, no clamping toward "normal"."""
    alarming = VitalReading(kind=VitalKind.BLOOD_PRESSURE_SYSTOLIC, value=200.0)
    stored = await store.record(alarming)

    assert stored.value == 200.0
    fetched = await store.get(alarming.id)
    assert fetched is not None
    assert fetched.value == 200.0, "the value must round-trip exactly, not be adjusted"


async def test_the_rejection_message_never_carries_the_measured_value(store):
    """Health values are SENSITIVE content (CLAUDE.md rule: log identifiers, never
    content). A caller's generic `except Exception: logger.exception(...)` will capture
    `str(exc)` verbatim — if the raw number were in that string, a routine log line
    would leak a person's blood pressure. The kind and the bounds (published constants,
    not the user's data) are fine to include; the value is not."""
    with pytest.raises(ImplausibleReadingError) as excinfo:
        await store.record(VitalReading(kind=VitalKind.SPO2, value=5.0))

    message = str(excinfo.value)
    assert "5.0" not in message
    assert "spo2" in message  # the kind is an identifier, not content — fine to log


async def test_boundary_values_are_accepted_not_off_by_one_rejected(store):
    """PLAUSIBLE is inclusive at both ends — a reading exactly at the boundary is a
    real, if extreme, measurement and must not be rejected by a stray strict inequality."""
    low, high = 50.0, 260.0  # ars_protocol.health.PLAUSIBLE[BLOOD_PRESSURE_SYSTOLIC]
    at_low = await store.record(VitalReading(kind=VitalKind.BLOOD_PRESSURE_SYSTOLIC, value=low))
    at_high = await store.record(
        VitalReading(kind=VitalKind.BLOOD_PRESSURE_SYSTOLIC, value=high)
    )
    assert at_low.value == low
    assert at_high.value == high


async def test_record_many_rejects_one_bad_reading_without_losing_the_rest(store):
    """A device's buffer is not all-or-nothing: one implausible entry (a cuff that
    slipped for one measurement in a session) must not cost every other, valid reading
    sitting next to it in the same upload."""
    good_a = VitalReading(kind=VitalKind.HEART_RATE, value=72.0)
    bad = VitalReading(kind=VitalKind.HEART_RATE, value=900.0)  # PLAUSIBLE high is 250
    good_b = VitalReading(kind=VitalKind.HEART_RATE, value=75.0)

    result = await store.record_many([good_a, bad, good_b])

    assert {r.id for r in result.accepted} == {good_a.id, good_b.id}
    assert len(result.rejected) == 1
    rejected_reading, reason = result.rejected[0]
    assert rejected_reading.id == bad.id
    assert "900.0" not in reason, "rejection reason must not leak the value either"
