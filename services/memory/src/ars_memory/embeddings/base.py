"""Embedding backend interface.

Kept separate from `ars_core.interfaces` on purpose: embeddings are an implementation
detail of *this* service's recall, not a seam another service plugs into. If that
changes (e.g. compute wants embeddings directly) it graduates to ars_core.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

import numpy as np

EMBEDDING_DIM = 384
"""Fixed across every backend (real and mock) so the schema (data/migrations/0004,
vec0's `float[384]`) never has to branch on which backend produced a vector. This
matches paraphrase-multilingual-MiniLM-L12-v2's output width."""


class EmbeddingBackend(ABC):
    """Turns text into a fixed-width vector for semantic recall.

    Every implementation must be:
      * deterministic-enough that recall tests are stable (the hash backend is fully
        deterministic; the real backend is deterministic given fixed model weights);
      * language-agnostic at the interface level — RO and EN text both go through
        `embed`, with no language branch here. Cross-lingual behaviour is a property of
        *which backend* is loaded, not of this interface.
    """

    @property
    @abstractmethod
    def dim(self) -> int: ...

    @abstractmethod
    async def embed(self, text: str) -> np.ndarray:
        """Returns a `(dim,)` float32 vector. Implementations should L2-normalize so
        callers can use plain dot product as cosine similarity."""

    async def embed_many(self, texts: Sequence[str]) -> list[np.ndarray]:
        """Default: sequential `embed`. Real backends should override for batching."""
        return [await self.embed(t) for t in texts]


def normalize(vector: np.ndarray) -> np.ndarray:
    """L2-normalize, guarding the zero-vector edge case (empty/whitespace-only text)."""
    norm = float(np.linalg.norm(vector))
    if norm < 1e-12:
        return vector.astype(np.float32)
    return (vector / norm).astype(np.float32)
