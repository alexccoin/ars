"""Correction by supersession, mirroring `ars_memory`'s `test_supersession.py`: "my
blood pressure was 180" and "it was recorded as 180 and corrected" are different facts.
A clinician reading this store later needs both — the wrong number a device actually
reported, and the fact that a human decided it was wrong — which is only possible if the
old row is never overwritten in place.
"""

from __future__ import annotations

import pytest
from ars_protocol import VitalKind, VitalReading


def _hr(value: float) -> VitalReading:
    return VitalReading(kind=VitalKind.HEART_RATE, value=value)


async def test_correction_resolves_to_the_new_reading(store):
    old = await store.record(_hr(180.0))  # mis-fired sensor
    new = await store.record(_hr(72.0))  # the real reading, moments later
    await store.record(old.model_copy(update={"superseded_by": new.id}))

    assert (await store.latest(VitalKind.HEART_RATE)).id == new.id


async def test_the_old_row_is_never_overwritten(store):
    """The wrong number must still be readable through `history()` — deleting the
    evidence that a device misfired is a different operation (`forget`) from correcting
    the record, and this store must not conflate the two."""
    old = await store.record(_hr(180.0))
    new = await store.record(_hr(72.0))
    await store.record(old.model_copy(update={"superseded_by": new.id}))

    history = await store.history(new.id)
    values_by_id = {r.id: r.value for r in history}

    assert values_by_id[old.id] == 180.0, "the mis-fired value must stay exactly as recorded"
    assert values_by_id[new.id] == 72.0
    assert len(history) == 2


async def test_cannot_change_the_value_of_an_existing_reading(store):
    """`VitalReading` is frozen and this store enforces the same rule at the database
    level: a reading's measurement never changes after the fact. Only `superseded_by`
    is allowed to move, and only from NULL to a value."""
    original = await store.record(_hr(72.0))
    tampered = original.model_copy(update={"value": 999.0})

    with pytest.raises(ValueError):
        await store.record(tampered)


async def test_cannot_re_supersede_once_set(store):
    old = await store.record(_hr(180.0))
    correction_a = await store.record(_hr(72.0))
    correction_b = await store.record(_hr(75.0))

    await store.record(old.model_copy(update={"superseded_by": correction_a.id}))

    with pytest.raises(ValueError):
        await store.record(old.model_copy(update={"superseded_by": correction_b.id}))


async def test_record_is_idempotent_for_an_identical_supersession(store):
    old = await store.record(_hr(180.0))
    new = await store.record(_hr(72.0))
    updated = old.model_copy(update={"superseded_by": new.id})

    first = await store.record(updated)
    second = await store.record(updated)

    assert first.superseded_by == new.id
    assert second.superseded_by == new.id
