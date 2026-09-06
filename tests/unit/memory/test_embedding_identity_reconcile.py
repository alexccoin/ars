"""An index built by one embedding model must not be queried by another.

Vectors from two models share a coordinate space the way two people's handwriting shares
an alphabet: the numbers line up and mean nothing. Nothing fails — recall just returns the
wrong passages, confidently. So the store records which model produced the index and
re-embeds when that changes, and this is the test that it actually does.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from ars_memory import MemoryConfig, SqliteMemoryStore
from ars_memory.embeddings.hash_backend import HashEmbeddingBackend
from ars_protocol import (
    Language, MemoryKind, MemoryRecord, Provenance, Sensitivity, SourceKind, TrustLevel,
)


class _MirrorBackend(HashEmbeddingBackend):
    """A different model, in the only way that matters here: different vectors for the
    same text, and an identity that says so."""

    @property
    def identity(self) -> str:
        return "test:mirror"

    async def embed(self, text: str):
        return -np.asarray(await super().embed(text), dtype=np.float32)


def _record(text: str) -> MemoryRecord:
    return MemoryRecord(
        kind=MemoryKind.DOCUMENT,
        text=text,
        language=Language.RO,
        sensitivity=Sensitivity.PERSONAL,
        provenance=Provenance(
            source=SourceKind.LOCAL_FILE, trust=TrustLevel.USER_DATA, label="lease.txt"
        ),
    )


async def _meta(store: SqliteMemoryStore, key: str) -> str | None:
    cursor = await store._conn.execute("SELECT value FROM memory_meta WHERE key = ?", (key,))
    row = await cursor.fetchone()
    return row["value"] if row else None


@pytest.mark.asyncio
async def test_changing_the_model_re_embeds_the_index(tmp_path: Path) -> None:
    config = MemoryConfig(data_dir=tmp_path, embedding_backend="hash")
    texts = ["Chiria pe lună este de 4.200 lei.", "Garanția este de 8.400 lei."]

    original = await SqliteMemoryStore.open(config)
    try:
        for text in texts:
            await original.remember(_record(text))
        assert await _meta(original, "embedding_identity") == HashEmbeddingBackend().identity
    finally:
        await original.close()

    mirror = _MirrorBackend()
    reopened = await SqliteMemoryStore.open(config, embedding_backend=mirror)
    try:
        assert await _meta(reopened, "embedding_identity") == "test:mirror"

        # The stored vectors are the new model's, not the old ones: a query embedded by
        # the new backend scores ~1.0 against its own record. Had the index been left
        # alone it would score ~-1.0 — the exact sign flip _MirrorBackend applies.
        qvec = await mirror.embed_query(texts[0])
        hits = await reopened._vector_index.search(qvec, limit=1)
        assert hits, "the re-embedded index returned nothing"
        _, similarity = hits[0]
        assert similarity > 0.99
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_reopening_with_the_same_model_re_embeds_nothing(tmp_path: Path) -> None:
    config = MemoryConfig(data_dir=tmp_path, embedding_backend="hash")
    store = await SqliteMemoryStore.open(config)
    try:
        await store.remember(_record("Chiria pe lună este de 4.200 lei."))
    finally:
        await store.close()

    reopened = await SqliteMemoryStore.open(config)
    try:
        assert await reopened._reconcile_embedding_model() == 0
    finally:
        await reopened.close()
