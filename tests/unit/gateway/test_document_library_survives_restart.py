"""What the library shows must match what the store actually holds — across a restart.

Two failures are the same lie in opposite directions: a document that still answers
questions after being deleted, and a document that answers questions but has vanished
from the library because the process restarted. Both are tested here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ars_gateway.documents import DocumentLibrary
from ars_memory import MemoryConfig, SqliteMemoryStore

LEASE = (
    "CONTRACT DE ÎNCHIRIERE — Strada Republicii 42\n\n"
    "Chiria pe lună este de 4.200 lei, plătibilă până în data de 5 a fiecărei luni.\n"
    "Garanția este de două chirii, adică 8.400 lei, returnabilă la predare.\n"
)


@pytest.fixture
def config(tmp_path: Path) -> MemoryConfig:
    return MemoryConfig(data_dir=tmp_path, embedding_backend="hash")


@pytest.mark.asyncio
async def test_a_learned_document_is_still_there_after_a_restart(config: MemoryConfig) -> None:
    store = await SqliteMemoryStore.open(config)
    try:
        doc = await DocumentLibrary(store).learn("contract.txt", LEASE.encode())
    finally:
        await store.close()

    reopened = await SqliteMemoryStore.open(config)
    try:
        library = DocumentLibrary(reopened)
        assert await library.rehydrate() == 1

        (entry,) = library.catalogue()
        assert entry["id"] == doc.id
        assert entry["name"] == "contract.txt"
        assert entry["chunks"] == doc.chunks
        assert entry["chars"] == doc.chars
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_forgetting_a_document_survives_a_restart_too(config: MemoryConfig) -> None:
    store = await SqliteMemoryStore.open(config)
    try:
        library = DocumentLibrary(store)
        doc = await library.learn("contract.txt", LEASE.encode())
        assert await library.forget(doc.id) == doc.chunks
    finally:
        await store.close()

    reopened = await SqliteMemoryStore.open(config)
    try:
        assert await DocumentLibrary(reopened).rehydrate() == 0
        assert await reopened.recall("chiria pe lună") == ()
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_a_document_whose_chunks_vanished_does_not_come_back(config: MemoryConfig) -> None:
    """Retention, or an interrupted delete, can take the chunks out from underneath the
    catalogue. Showing the user a document that can no longer answer anything is worse
    than showing nothing."""
    store = await SqliteMemoryStore.open(config)
    try:
        library = DocumentLibrary(store)
        doc = await library.learn("contract.txt", LEASE.encode())
        for record_id in library._chunk_ids[doc.id]:
            await store.forget(record_id=record_id)
    finally:
        await store.close()

    reopened = await SqliteMemoryStore.open(config)
    try:
        assert await DocumentLibrary(reopened).rehydrate() == 0
        assert await reopened.meta_get(f"document:{doc.id}") is None
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_chunks_stored_before_the_catalogue_existed_are_adopted(
    config: MemoryConfig,
) -> None:
    """Documents learned by an older build have chunks and no catalogue entry: they
    answer questions while showing up nowhere in the library. Rehydrating adopts them,
    and says "unknown" for the two things that genuinely cannot be recovered."""
    store = await SqliteMemoryStore.open(config)
    try:
        library = DocumentLibrary(store)
        doc = await library.learn("contract.txt", LEASE.encode())
        # Exactly the pre-persistence state: the chunks, without the catalogue entry.
        assert await store.meta_delete(f"document:{doc.id}")
    finally:
        await store.close()

    reopened = await SqliteMemoryStore.open(config)
    try:
        library = DocumentLibrary(reopened)
        assert await library.rehydrate() == 1

        (entry,) = library.catalogue()
        assert entry["id"] == doc.id
        assert entry["name"] == "contract.txt"
        assert entry["chunks"] == doc.chunks
        assert entry["pages"] is None

        # Adoption is durable: the next start reads a real catalogue entry, not orphans.
        assert await reopened.meta_get(f"document:{doc.id}") is not None
        assert await library.forget(doc.id) == doc.chunks
    finally:
        await reopened.close()

    final = await SqliteMemoryStore.open(config)
    try:
        assert await DocumentLibrary(final).rehydrate() == 0
    finally:
        await final.close()
