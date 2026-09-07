from __future__ import annotations

from pathlib import Path

import pytest_asyncio
from ars_medical import MedicalConfig, SqliteMedicalStore


@pytest_asyncio.fixture
async def store(tmp_path: Path):
    config = MedicalConfig(data_dir=tmp_path)
    medical_store = await SqliteMedicalStore.open(config)
    try:
        yield medical_store
    finally:
        await medical_store.close()


@pytest_asyncio.fixture
async def bare_store(tmp_path: Path):
    """No default reference ranges seeded — for tests that want to control the range
    table exactly, e.g. asserting `findings()` cites the one range they added."""
    config = MedicalConfig(data_dir=tmp_path, seed_default_ranges=False)
    medical_store = await SqliteMedicalStore.open(config)
    try:
        yield medical_store
    finally:
        await medical_store.close()


@pytest_asyncio.fixture
async def config(tmp_path: Path) -> MedicalConfig:
    return MedicalConfig(data_dir=tmp_path)
