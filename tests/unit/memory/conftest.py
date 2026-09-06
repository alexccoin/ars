from __future__ import annotations

from pathlib import Path

import pytest_asyncio
from ars_memory import MemoryConfig, SqliteMemoryStore


@pytest_asyncio.fixture
async def store(tmp_path: Path):
    config = MemoryConfig(data_dir=tmp_path, embedding_backend="hash")
    memory_store = await SqliteMemoryStore.open(config)
    try:
        yield memory_store
    finally:
        await memory_store.close()


@pytest_asyncio.fixture
async def config(tmp_path: Path) -> MemoryConfig:
    return MemoryConfig(data_dir=tmp_path, embedding_backend="hash")
