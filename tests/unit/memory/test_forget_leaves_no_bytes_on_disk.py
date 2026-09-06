"""Deletion must remove the bytes, not just the rows.

Regression test for a real defect: `forget()` deleted the row and every index entry —
the table-level test passed — but the plaintext stayed in `memory.db-wal`, because the
original INSERT frames sit in the write-ahead log until it is checkpointed. `strings`
on the WAL recovered a deleted SENSITIVE record.

For a store of private memories, "delete my data" has to mean the bytes are gone.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ars_memory.config import MemoryConfig
from ars_memory.store import SqliteMemoryStore
from ars_protocol import (
    Language,
    MemoryKind,
    MemoryRecord,
    Provenance,
    Sensitivity,
    SourceKind,
    TrustLevel,
)

SECRET = "CANARY-PSYCHIATRIST-DR-IONESCU-TUESDAY-1543"  # noqa: S105 - a canary, not a credential


def _files_containing(directory: Path, needle: str) -> list[str]:
    """Every file the store touches — main db, -wal and -shm included."""
    return [
        p.name for p in directory.iterdir()
        if p.is_file() and needle.encode() in p.read_bytes()
    ]


@pytest.mark.asyncio
async def test_forget_removes_the_plaintext_from_every_file_on_disk(tmp_path: Path) -> None:
    store = await SqliteMemoryStore.open(
        MemoryConfig(db_path_override=tmp_path / "memory.db")
    )
    try:
        record = MemoryRecord(
            kind=MemoryKind.FACT,
            text=SECRET,
            language=Language.EN,
            sensitivity=Sensitivity.SENSITIVE,
            provenance=Provenance(source=SourceKind.MICROPHONE, trust=TrustLevel.USER),
        )
        await store.remember(record)
        assert _files_containing(tmp_path, SECRET), (
            "test is not exercising anything: the secret was never written to disk"
        )

        assert await store.forget(record_id=record.id) == 1

        leaked = _files_containing(tmp_path, SECRET)
        assert leaked == [], (
            f"deleted SENSITIVE text is still recoverable from {leaked}; "
            "secure_delete and a WAL checkpoint are required after forget()"
        )
    finally:
        await store.close()

    assert _files_containing(tmp_path, SECRET) == [], "plaintext reappeared after close"


@pytest.mark.asyncio
async def test_db_path_cannot_be_set_silently(tmp_path: Path) -> None:
    """`db_path` is a derived property. Passing it as a kwarg used to be dropped by
    pydantic, so a caller that thought it had isolated the database was in fact writing
    private memories to the default location. That must fail loudly."""
    with pytest.raises(ValueError, match="db_path_override"):
        MemoryConfig(db_path=tmp_path / "elsewhere.db")

    cfg = MemoryConfig(db_path_override=tmp_path / "elsewhere.db")
    assert cfg.db_path == tmp_path / "elsewhere.db"
