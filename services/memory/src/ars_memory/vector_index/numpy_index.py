"""Fallback vector index: raw float32 blobs in `memory_embeddings`
(data/migrations/0003), brute-force cosine similarity in Python via numpy.

This is the documented fallback for a machine where the sqlite-vec loadable extension
does not load (extension loading can be compiled out of some Python/SQLite builds).
Brute force is the right tradeoff at this service's scale — personal memory is
thousands of records, not millions — so there is no ANN index to keep consistent, no
approximate-result surprises, and no extra native dependency to worry about when this
path is active.
"""

from __future__ import annotations

import aiosqlite
import numpy as np

from .base import VectorIndex


class NumpyBruteForceIndex(VectorIndex):
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def upsert(self, rowid: int, vector: np.ndarray) -> None:
        vector = np.asarray(vector, dtype=np.float32)
        await self._conn.execute(
            "INSERT INTO memory_embeddings (memory_rowid, dim, embedding) VALUES (?, ?, ?) "
            "ON CONFLICT(memory_rowid) DO UPDATE SET "
            "dim = excluded.dim, embedding = excluded.embedding",
            (rowid, int(vector.shape[0]), vector.tobytes()),
        )

    async def delete(self, rowid: int) -> None:
        await self._conn.execute("DELETE FROM memory_embeddings WHERE memory_rowid = ?", (rowid,))

    async def search(self, query: np.ndarray, limit: int) -> list[tuple[int, float]]:
        query = np.asarray(query, dtype=np.float32)
        q_norm = float(np.linalg.norm(query))
        if q_norm < 1e-12:
            return []
        cursor = await self._conn.execute(
            "SELECT memory_rowid, dim, embedding FROM memory_embeddings"
        )
        rows = await cursor.fetchall()
        scored: list[tuple[int, float]] = []
        for rowid, dim, blob in rows:
            vec = np.frombuffer(blob, dtype=np.float32)
            if vec.shape[0] != dim:
                continue
            v_norm = float(np.linalg.norm(vec))
            if v_norm < 1e-12:
                continue
            similarity = float(np.dot(query, vec) / (q_norm * v_norm))
            scored.append((rowid, similarity))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:limit]

    async def contains(self, rowid: int) -> bool:
        cursor = await self._conn.execute(
            "SELECT 1 FROM memory_embeddings WHERE memory_rowid = ?", (rowid,)
        )
        return (await cursor.fetchone()) is not None
