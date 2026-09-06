"""Real multilingual embedding backend — sentence-transformers.

Imported lazily and only from inside this module (never at package `__init__` scope),
per CLAUDE.md rule 2: no vendor SDK outside a backend implementation. The model is
loaded on first call to `embed`/`embed_many`, not in `__init__`, so constructing a
`SqliteMemoryStore` never touches disk/network by itself — only actually calling
`remember`/`recall` with this backend selected does, and only once (the loaded model is
cached on the instance).

The default is `intfloat/multilingual-e5-small`, chosen by measurement rather than
familiarity — see `research/benchmarks/retrieval_calibration.py`, which scores every
question against the passage that answers it and against passages that do not, in
English and Romanian:

    paraphrase-multilingual-MiniLM-L12-v2   relevant 0.218-0.647   irrelevant up to 0.407
    intfloat/multilingual-e5-small          relevant 0.825-0.883   irrelevant up to 0.817

The first model's two populations *overlap*: a Romanian question scored 0.218 against
the document that answers it while an unrelated question scored 0.407 against one that
does not. No threshold splits those, so the document tier could not be made safe with
it — which is exactly the bug where "what is the capital of Portugal?" was answered from
an employment contract. e5 separates them, and separates them equally well in both
languages (weakest true match 0.825 EN / 0.826 RO), which is what rule 5 requires.

Both are 384-dimensional, so this swap needs no schema change — but it does invalidate
every vector already stored, which `SqliteMemoryStore` handles by re-embedding when the
recorded model identity no longer matches.

e5 models are asymmetric: they are trained with `query: ` in front of the question and
`passage: ` in front of the text, and lose most of their advantage without them. That is
why `embed_query`/`embed_passage` exist on the interface.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import numpy as np

from .base import EMBEDDING_DIM, EmbeddingBackend, normalize

DEFAULT_MODEL = "intfloat/multilingual-e5-small"

E5_QUERY_PREFIX = "query: "
E5_PASSAGE_PREFIX = "passage: "


class SentenceTransformerEmbeddingBackend(EmbeddingBackend):
    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        *,
        query_prefix: str | None = None,
        passage_prefix: str | None = None,
    ) -> None:
        self._model_name = model_name
        # Inferred from the model name so callers cannot forget, overridable so a future
        # model with a different convention does not need a code change here.
        e5 = "e5" in model_name.lower()
        self._query_prefix = query_prefix if query_prefix is not None else (E5_QUERY_PREFIX if e5 else "")
        self._passage_prefix = (
            passage_prefix if passage_prefix is not None else (E5_PASSAGE_PREFIX if e5 else "")
        )
        self._model: Any | None = None
        self._lock = asyncio.Lock()

    @property
    def dim(self) -> int:
        return EMBEDDING_DIM

    @property
    def identity(self) -> str:
        return f"sentence_transformer:{self._model_name}"

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

    async def embed_query(self, text: str) -> np.ndarray:
        return (await self.embed_many([self._query_prefix + text]))[0]

    async def embed_passage(self, text: str) -> np.ndarray:
        return (await self.embed_many([self._passage_prefix + text]))[0]

    async def embed_passages(self, texts: Sequence[str]) -> list[np.ndarray]:
        return await self.embed_many([self._passage_prefix + t for t in texts])
