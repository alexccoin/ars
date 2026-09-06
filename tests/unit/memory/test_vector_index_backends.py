"""Both `VectorIndex` implementations must satisfy the same contract, and the store
must behave identically regardless of which one backs it — this is what makes the
numpy fallback a safe substitute on a machine where sqlite-vec's `vec0` module does not
load (this repo's arm64 `.venv` does load it; see `ars_memory.store` module docstring).
"""

from __future__ import annotations

import numpy as np
import pytest
from ars_memory import MemoryConfig, SqliteMemoryStore
from ars_memory.vector_index.numpy_index import NumpyBruteForceIndex
from ars_protocol import (
    Language,
    MemoryKind,
    MemoryRecord,
    Provenance,
    Sensitivity,
    SourceKind,
    TrustLevel,
)


@pytest.fixture(params=["sqlite_vec", "numpy"])
def index_kind(request):
    return request.param


async def _make_index(kind: str, conn):
    if kind == "numpy":
        return NumpyBruteForceIndex(conn)
    from ars_memory.vector_index.sqlite_vec_index import SqliteVecIndex

    return SqliteVecIndex(conn)


async def test_vector_index_contract(tmp_path, index_kind):
    from ars_memory import db

    conn, vec_available = await db.open_connection(tmp_path / "idx.db")
    await db.migrate(conn)
    if index_kind == "sqlite_vec" and not vec_available:
        pytest.skip("sqlite-vec extension not available on this machine")
    index = await _make_index(index_kind, conn)

    a = np.zeros(384, dtype=np.float32)
    a[0] = 1.0
    b = np.zeros(384, dtype=np.float32)
    b[1] = 1.0
    c = np.zeros(384, dtype=np.float32)
    c[0] = 0.9
    c[1] = 0.1
    c = c / np.linalg.norm(c)

    await index.upsert(1, a)
    await index.upsert(2, b)
    await index.upsert(3, c)

    assert await index.contains(1)
    assert await index.contains(2)

    results = await index.search(a, limit=3)
    result_ids = [rowid for rowid, _ in results]
    assert result_ids[0] == 1  # identical vector must rank first
    assert 2 in result_ids

    await index.delete(2)
    assert not await index.contains(2)
    results_after_delete = await index.search(a, limit=3)
    assert 2 not in [rowid for rowid, _ in results_after_delete]

    await conn.close()


async def test_store_behaves_identically_when_forced_onto_numpy_fallback(tmp_path):
    from ars_memory import db

    config = MemoryConfig(data_dir=tmp_path, embedding_backend="hash")
    conn, _ = await db.open_connection(config.db_path)
    await db.migrate(conn)

    from ars_memory.embeddings.hash_backend import HashEmbeddingBackend

    store = SqliteMemoryStore(
        conn=conn,
        vector_index=NumpyBruteForceIndex(conn),
        embedding_backend=HashEmbeddingBackend(),
        config=config,
        vec_backend_active=False,
    )

    ro = await store.remember(
        MemoryRecord(
            kind=MemoryKind.FACT,
            text="Locuiesc în Cluj de trei ani.",
            language=Language.RO,
            sensitivity=Sensitivity.PERSONAL,
            provenance=Provenance(source=SourceKind.MICROPHONE, trust=TrustLevel.USER),
        )
    )
    results = await store.recall("Where do I live, is it Cluj?", limit=3)
    assert results[0].id == ro.id

    rowid = await store.debug_rowid_for(ro.id)
    deleted = await store.forget(record_id=ro.id)
    assert deleted == 1
    assert not await store.vector_index_contains_rowid(rowid)

    await store.close()
