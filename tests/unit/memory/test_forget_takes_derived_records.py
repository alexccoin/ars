"""Deleting a record deletes what was derived from it.

A translated copy of a passage exists only because the passage does. If `forget()` leaves
it behind, a document the user deleted keeps answering questions through its translation —
and the user has no way to discover that, because they deleted the thing they knew about.
Non-negotiable #7 says deletion must actually delete, and "actually" has to include the
copies the system made on its own initiative.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ars_memory import MemoryConfig, SqliteMemoryStore
from ars_protocol import (
    Language, MemoryKind, MemoryRecord, Provenance, Sensitivity, SourceKind, TrustLevel,
)

GERMAN = "Die Miete beträgt monatlich 1.200 Euro und ist zum Dritten fällig."
ENGLISH = "The rent is 1,200 euro per month and is due on the third."
ROMANIAN = "Chiria este de 1.200 de euro pe lună și se plătește pe data de trei."


def _record(text: str, language: Language, origin_id: str | None = None) -> MemoryRecord:
    return MemoryRecord(
        kind=MemoryKind.DOCUMENT,
        text=text,
        language=language,
        sensitivity=Sensitivity.PERSONAL,
        provenance=Provenance(
            source=SourceKind.LOCAL_FILE, trust=TrustLevel.USER_DATA, label="Mietvertrag.pdf"
        ),
        origin_id=origin_id,
    )


@pytest.fixture
def config(tmp_path: Path) -> MemoryConfig:
    return MemoryConfig(data_dir=tmp_path, embedding_backend="hash")


@pytest.mark.asyncio
async def test_forgetting_a_passage_forgets_its_translations(config: MemoryConfig) -> None:
    store = await SqliteMemoryStore.open(config)
    try:
        source = await store.remember(_record(GERMAN, Language.DE))
        await store.remember(_record(ENGLISH, Language.EN, origin_id=source.id))
        await store.remember(_record(ROMANIAN, Language.RO, origin_id=source.id))

        removed = await store.forget(record_id=source.id)

        assert removed == 3, "the translations survived the passage they came from"
        assert await store.recall("Miete") == ()
        assert await store.recall("rent") == ()
        assert await store.recall("chiria") == ()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_derivation_is_followed_all_the_way_down(config: MemoryConfig) -> None:
    """A derived record can have derivatives of its own — a summary of a translation. One
    surviving link is enough to keep deleted text searchable."""
    store = await SqliteMemoryStore.open(config)
    try:
        source = await store.remember(_record(GERMAN, Language.DE))
        english = await store.remember(_record(ENGLISH, Language.EN, origin_id=source.id))
        await store.remember(_record("Rent: 1,200 EUR monthly.", Language.EN,
                                     origin_id=english.id))

        assert await store.forget(record_id=source.id) == 3
        assert await store.recall("Rent") == ()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_an_unrelated_record_is_left_alone(config: MemoryConfig) -> None:
    store = await SqliteMemoryStore.open(config)
    try:
        source = await store.remember(_record(GERMAN, Language.DE))
        await store.remember(_record(ENGLISH, Language.EN, origin_id=source.id))
        keeper = await store.remember(_record("Der Stellplatz kostet 80 Euro.", Language.DE))

        assert await store.forget(record_id=source.id) == 2
        assert await store.exists(keeper.id)
    finally:
        await store.close()
