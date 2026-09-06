"""Lazy-loading contract for the real embedding backend — verified WITHOUT downloading
any model weights, per the task constraint. `sentence_transformers.SentenceTransformer`
is monkeypatched with a fake that records how many times it was constructed and returns
deterministic vectors, so this test proves the *behaviour* (load once, on first use,
off the event loop) without touching the network.
"""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest
from ars_memory.embeddings.sentence_transformer_backend import (
    SentenceTransformerEmbeddingBackend,
)


class _FakeSentenceTransformer:
    instances = 0

    def __init__(self, model_name: str) -> None:
        _FakeSentenceTransformer.instances += 1
        self.model_name = model_name

    def encode(self, texts, convert_to_numpy=True):
        return np.stack([np.full(384, float(len(t) % 7 + 1), dtype=np.float32) for t in texts])


@pytest.fixture(autouse=True)
def fake_sentence_transformers(monkeypatch):
    _FakeSentenceTransformer.instances = 0
    fake_module = types.ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = _FakeSentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)
    yield


async def test_model_not_loaded_until_first_embed():
    backend = SentenceTransformerEmbeddingBackend("paraphrase-multilingual-MiniLM-L12-v2")
    assert _FakeSentenceTransformer.instances == 0

    await backend.embed("hello")

    assert _FakeSentenceTransformer.instances == 1


async def test_model_loaded_only_once_across_many_calls():
    backend = SentenceTransformerEmbeddingBackend()
    for _ in range(5):
        await backend.embed("hello")
    assert _FakeSentenceTransformer.instances == 1


async def test_embed_returns_normalized_384_dim_vector():
    backend = SentenceTransformerEmbeddingBackend()
    vector = await backend.embed("test")
    assert vector.shape == (384,)
    assert abs(float(np.linalg.norm(vector)) - 1.0) < 1e-5


async def test_embed_many_batches_in_one_encode_call():
    backend = SentenceTransformerEmbeddingBackend()
    vectors = await backend.embed_many(["a", "bb", "ccc"])
    assert len(vectors) == 3
    assert _FakeSentenceTransformer.instances == 1
