"""Real multilingual embedding backend — sentence-transformers.

Imported lazily and only from inside this module (never at package `__init__` scope),
per CLAUDE.md rule 2: no vendor SDK outside a backend implementation. The model is
loaded on first call to `embed`/`embed_many`, not in `__init__`, so constructing a
`SqliteMemoryStore` never touches disk/network by itself — only actually calling
`remember`/`recall` with this backend selected does, and only once (the loaded model is
cached on the instance).

`paraphrase-multilingual-MiniLM-L12-v2` (the default) is trained across 50+ languages
including English and Romanian and produces 384-dim embeddings — matching
`EMBEDDING_DIM` and the fixed `vec0(embedding float[384])` schema, so swapping this in
for the hash backend requires no migration change.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import numpy as np

from .base import EMBEDDING_DIM, EmbeddingBackend, normalize


class SentenceTransformerEmbeddingBackend(EmbeddingBackend):
    def __init__(self, model_name: str = "paraphrase-multilingual-MiniLM-L12-v2") -> None:
        self._model_name = model_name
        self._model: Any | None = None
        self._lock = asyncio.Lock()

    @property
    def dim(self) -> int:
        return EMBEDDING_DIM

    async def _get_model(self) -> Any:
        if self._model is not None:
            return self._model
        async with self._lock:
            if self._model is None:
                self._model = await asyncio.to_thread(self._load_model)
        return self._model

    def _load_model(self) -> Any:
        # Deliberately the only place this package imports sentence_transformers.
        from sentence_transformers import SentenceTransformer

        return SentenceTransformer(self._model_name)

    async def embed(self, text: str) -> np.ndarray:
        vectors = await self.embed_many([text])
        return vectors[0]

    async def embed_many(self, texts: Sequence[str]) -> list[np.ndarray]:
        if not texts:
            return []
        model = await self._get_model()
        raw = await asyncio.to_thread(model.encode, list(texts), convert_to_numpy=True)
        return [normalize(np.asarray(v, dtype=np.float32)) for v in raw]
