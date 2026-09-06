"""Questions and passages must not be embedded the same way.

e5-family models are trained with `query: ` in front of the question and `passage: ` in
front of the text. Getting that backwards — or dropping it — does not raise: it just
retrieves worse, quietly, which is the kind of bug that only shows up as "A.R.S answered
from the wrong contract". So the prefixes are asserted here, and so is the store's use of
the right side of the interface on the right call.

No weights are downloaded: `SentenceTransformer` is a fake that records the text it was
handed, which is exactly what is under test.
"""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest
from ars_memory.embeddings.hash_backend import HashEmbeddingBackend
from ars_memory.embeddings.sentence_transformer_backend import (
    SentenceTransformerEmbeddingBackend,
)


class _RecordingModel:
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.seen: list[str] = []

    def encode(self, texts, convert_to_numpy=True):
        self.seen.extend(texts)
        return np.stack([np.full(384, float(len(t) % 7 + 1), dtype=np.float32) for t in texts])


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    class _Model(_RecordingModel):
        def encode(self, texts, convert_to_numpy=True):
            seen.extend(texts)
            return super().encode(texts, convert_to_numpy)

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = _Model  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    return seen


@pytest.mark.asyncio
async def test_e5_model_gets_query_and_passage_prefixes(recorded: list[str]) -> None:
    backend = SentenceTransformerEmbeddingBackend("intfloat/multilingual-e5-small")
    await backend.embed_query("Cât este chiria pe lună?")
    await backend.embed_passage("Chiria pe lună este de 4.200 lei.")

    assert recorded == [
        "query: Cât este chiria pe lună?",
        "passage: Chiria pe lună este de 4.200 lei.",
    ]


@pytest.mark.asyncio
async def test_symmetric_model_gets_no_prefixes(recorded: list[str]) -> None:
    backend = SentenceTransformerEmbeddingBackend("paraphrase-multilingual-MiniLM-L12-v2")
    await backend.embed_query("Cât este chiria pe lună?")
    await backend.embed_passage("Chiria pe lună este de 4.200 lei.")

    assert recorded == ["Cât este chiria pe lună?", "Chiria pe lună este de 4.200 lei."]


@pytest.mark.asyncio
async def test_batch_passages_are_prefixed_too(recorded: list[str]) -> None:
    backend = SentenceTransformerEmbeddingBackend("intfloat/multilingual-e5-small")
    await backend.embed_passages(["one", "two"])

    assert recorded == ["passage: one", "passage: two"]


@pytest.mark.asyncio
async def test_backend_that_does_not_care_is_unaffected() -> None:
    """A symmetric backend inherits the default and both sides agree — which is what
    makes the hash double usable as a stand-in at all."""
    backend = HashEmbeddingBackend()
    same = "Cât este chiria pe lună?"
    assert np.allclose(await backend.embed_query(same), await backend.embed_passage(same))
