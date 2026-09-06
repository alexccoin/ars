"""A.R.S semantic memory + learned behaviour — fully local.

`SqliteMemoryStore` is the only implementation of `ars_core.interfaces.MemoryStore` in
this service. See its module docstring for the sqlite-vec/numpy backend decision and
`ars_memory.ranking` for the hybrid recall formula.
"""

from __future__ import annotations

from .config import MemoryConfig
from .embeddings.base import EMBEDDING_DIM, EmbeddingBackend
from .embeddings.hash_backend import HashEmbeddingBackend
from .retention import RetentionReport, RetentionSweeper
from .store import SqliteMemoryStore

__all__ = [
    "EMBEDDING_DIM",
    "EmbeddingBackend",
    "HashEmbeddingBackend",
    "MemoryConfig",
    "RetentionReport",
    "RetentionSweeper",
    "SqliteMemoryStore",
]
