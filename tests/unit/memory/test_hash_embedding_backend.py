"""The deterministic mock backend: no download, stable across processes, unit length."""

from __future__ import annotations

import numpy as np
from ars_memory.embeddings.hash_backend import HashEmbeddingBackend


async def test_deterministic_across_calls():
    backend = HashEmbeddingBackend()
    v1 = await backend.embed("I live in Cluj")
    v2 = await backend.embed("I live in Cluj")
    assert np.allclose(v1, v2)


async def test_unit_length():
    backend = HashEmbeddingBackend()
    v = await backend.embed("Locuiesc în Cluj-Napoca")
    assert abs(float(np.linalg.norm(v)) - 1.0) < 1e-5


async def test_shared_token_yields_higher_similarity_than_unrelated_text():
    backend = HashEmbeddingBackend()
    a = await backend.embed("I live in Cluj")
    b = await backend.embed("Locuiesc în Cluj de mult timp")
    c = await backend.embed("The weather forecast predicts rain tomorrow")

    sim_shared = float(np.dot(a, b))
    sim_unrelated = float(np.dot(a, c))

    assert sim_shared > sim_unrelated


async def test_empty_text_does_not_crash():
    backend = HashEmbeddingBackend()
    v = await backend.embed("")
    assert v.shape == (backend.dim,)
