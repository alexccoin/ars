"""Migration runner: applies numbered migrations, is idempotent, and tolerates a
missing sqlite-vec module for the OPTIONAL migration without failing the whole run."""

from __future__ import annotations

from pathlib import Path

from ars_memory import db


async def test_migrate_applies_all_migrations_and_is_idempotent(tmp_path: Path):
    conn, _ = await db.open_connection(tmp_path / "m.db")
    try:
        first_run = await db.migrate(conn)
        assert first_run, "expected at least one migration to apply on a fresh db"

        second_run = await db.migrate(conn)
        assert second_run == [], "re-running migrate() must be a no-op"

        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'memory_records'"
        )
        assert await cursor.fetchone() is not None
    finally:
        await conn.close()


async def test_optional_migration_failure_does_not_abort_the_run(tmp_path: Path, monkeypatch):
    import sqlite3

    conn, _ = await db.open_connection(tmp_path / "m2.db")
    try:
        real_executescript = conn.executescript

        async def failing_executescript(sql: str):
            if "USING vec0" in sql:
                raise sqlite3.OperationalError("no such module: vec0")
            return await real_executescript(sql)

        monkeypatch.setattr(conn, "executescript", failing_executescript)

        applied = await db.migrate(conn)
        assert any("memory_records" in m for m in applied)
        assert not any("memory_vec" in m for m in applied)

        # the rest of the schema must still exist even though the optional one failed
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'observations'"
        )
        assert await cursor.fetchone() is not None
    finally:
        await conn.close()
