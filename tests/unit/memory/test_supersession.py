"""Correction by supersession (requirement 4): "I live in Cluj" -> "I moved to
Bucharest" must resolve to the new fact in recall, while the old row stays in the
database, unmodified, queryable through `history()`."""

from __future__ import annotations

import pytest
from ars_protocol import (
    Language,
    MemoryKind,
    MemoryRecord,
    Provenance,
    Sensitivity,
    SourceKind,
    TrustLevel,
)


def _fact(text: str) -> MemoryRecord:
    return MemoryRecord(
        kind=MemoryKind.FACT,
        text=text,
        language=Language.EN,
        sensitivity=Sensitivity.PERSONAL,
        provenance=Provenance(source=SourceKind.MICROPHONE, trust=TrustLevel.USER),
    )


async def test_correction_resolves_to_current_fact(store):
    old = await store.remember(_fact("I live in Cluj"))
    new = await store.remember(_fact("I moved to Bucharest"))

    # The correction: point the old record forward. `Model` is frozen, so this is a
    # copy-and-re-remember, never a mutation in place.
    superseded_old = old.model_copy(update={"superseded_by": new.id})
    await store.remember(superseded_old)

    results = await store.recall("Where do I live?", limit=5)
    result_ids = {r.id for r in results}

    assert new.id in result_ids
    assert old.id not in result_ids, "superseded record must not be returned by recall"


async def test_old_row_is_never_overwritten(store):
    old = await store.remember(_fact("I live in Cluj"))
    new = await store.remember(_fact("I moved to Bucharest"))
    await store.remember(old.model_copy(update={"superseded_by": new.id}))

    history = await store.history(new.id)
    texts_by_id = {r.id: r.text for r in history}

    assert texts_by_id[old.id] == "I live in Cluj", "old text must be untouched"
    assert texts_by_id[new.id] == "I moved to Bucharest"
    assert len(history) == 2


async def test_cannot_overwrite_text_of_existing_record(store):
    original = await store.remember(_fact("I live in Cluj"))
    tampered = original.model_copy(update={"text": "I live somewhere else"})

    with pytest.raises(ValueError):
        await store.remember(tampered)


async def test_cannot_change_superseded_by_once_set(store):
    old = await store.remember(_fact("I live in Cluj"))
    new_a = await store.remember(_fact("I moved to Bucharest"))
    new_b = await store.remember(_fact("I moved to Iasi"))

    await store.remember(old.model_copy(update={"superseded_by": new_a.id}))

    with pytest.raises(ValueError):
        await store.remember(old.model_copy(update={"superseded_by": new_b.id}))


async def test_remember_is_idempotent_for_identical_supersession(store):
    old = await store.remember(_fact("I live in Cluj"))
    new = await store.remember(_fact("I moved to Bucharest"))
    updated = old.model_copy(update={"superseded_by": new.id})

    first = await store.remember(updated)
    second = await store.remember(updated)

    assert first.superseded_by == new.id
    assert second.superseded_by == new.id
