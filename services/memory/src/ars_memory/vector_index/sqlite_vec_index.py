"""Primary vector index: sqlite-vec's `vec0` virtual table (data/migrations/0004),
configured with `distance_metric=cosine` so `distance` returned by a `MATCH` query is
`1 - cosine_similarity` directly (verified empirically: orthogonal unit vectors give
distance 1.0, identical vectors give 0.0, 45 degrees gives ~0.293 = 1 - cos(45deg)).

`sqlite_vec` (the Python package) is imported here and nowhere else in this service —
`ars_memory.db` probes whether the extension actually *loads* on this machine's SQLite
build and only constructs this class if it does; otherwise it falls back to
`NumpyBruteForceIndex` behind the same `VectorIndex` interface.
"""

from __future__ import annotations

import aiosqlite
import numpy as np

from .base import VectorIndex


class SqliteVecIndex(VectorIndex):
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def upsert(self, rowid: int, vector: np.ndarray) -> None:
        vector = np.asarray(vector, dtype=np.float32)
        # vec0 has no ON CONFLICT upsert; a plain re-insert of the same rowid errors,
        # so delete-then-insert to keep `upsert` semantics exactly like the fallback.
        await self._conn.execute("DELETE FROM memory_vec WHERE rowid = ?", (rowid,))
        await self._conn.execute(
            "INSERT INTO memory_vec (rowid, embedding) VALUES (?, ?)",
            (rowid, vector.tobytes()),
        )

    async def delete(self, rowid: int) -> None:
        await self._conn.execute("DELETE FROM memory_vec WHERE rowid = ?", (rowid,))

    async def search(self, query: np.ndarray, limit: int) -> list[tuple[int, float]]:
        if limit <= 0:
            return []
        query = np.asarray(query, dtype=np.float32)
        cursor = await self._conn.execute(
            "SELECT rowid, distance FROM memory_vec WHERE embedding MATCH ? AND k = ? "
            "ORDER BY distance",
            (query.tobytes(), limit),
        )
        rows = await cursor.fetchall()
        return [(int(rowid), 1.0 - float(distance)) for rowid, distance in rows]

    async def contains(self, rowid: int) -> bool:
        cursor = await self._conn.execute(
            "SELECT 1 FROM memory_vec WHERE rowid = ?", (rowid,)
        )
        return (await cursor.fetchone()) is not None
