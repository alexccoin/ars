"""Vector index interface — one seam, two implementations behind it.

`SqliteVecIndex` (sqlite-vec's `vec0` virtual table) is preferred and is what this
service actually installed with on arm64 (see the module docstring in
`ars_memory.store` for the install/probe result). `NumpyBruteForceIndex` is the
documented fallback for environments where the sqlite-vec loadable extension does not
compile/load, kept behind the exact same interface so `SqliteMemoryStore` never branches
on which one it has.

`rowid` throughout is SQLite's own integer rowid of the corresponding row in
`memory_records` (see data/migrations/0001) — not the protocol `MemoryRecord.id`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class VectorIndex(ABC):
    @abstractmethod
    async def upsert(self, rowid: int, vector: np.ndarray) -> None: ...

    @abstractmethod
    async def delete(self, rowid: int) -> None:
        """Must be a real delete — no tombstone, nothing left to find by scanning the
        underlying storage. `forget()` depends on this."""

    @abstractmethod
    async def search(self, query: np.ndarray, limit: int) -> list[tuple[int, float]]:
        """Returns up to `limit` `(rowid, cosine_similarity)` pairs, best first.
        `cosine_similarity` is in `[-1, 1]`; callers clamp to `[0, 1]` before ranking."""

    @abstractmethod
    async def contains(self, rowid: int) -> bool:
        """For tests: proves a rowid's vector is (or is not) present."""
