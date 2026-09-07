"""Connection management + schema migration for the medical (vitals) database.

Migrations live at `data/migrations/medical/`, a subdirectory of the shared
`data/migrations/` root rather than the flat root itself where `ars_memory`'s files
sit. That is a deliberate deviation, so the reasoning is here rather than only in a PR
description:

`ars_memory.db.load_migrations` globs `*.up.sql` in whatever directory it is pointed at,
non-recursively, with no per-service filter — it was written to serve exactly one
database. If this service's migrations were dropped as flat files into that same root
directory, two things would both quietly happen the next time either store opens:
`SqliteMemoryStore.open()` would glob them up too and create empty `vital_readings` /
`reference_ranges` tables inside `memory.db`, and this store's own loader, pointed at
the same root, would just as blindly try to run `0001_memory_records` through
`0008_memory_origin` — including an FTS5 virtual table and an optional sqlite-vec one —
against `medical.db`. Nothing would leak (neither service's code ever addresses the
other's tables, so no data crosses), but a health-data store ending up with the entire
memory subsystem's schema sitting inside it — or vice versa — contradicts "vitals live
in their own SQLite database" and is exactly the kind of accident this subsystem should
not risk for a directory-naming convenience.

`services/auth` already has precedent for this in this exact repo:
`services/auth/data/migrations/<component>/`, one subdirectory per component, each with
its own file sequence. `medical/` follows the same shape. Numbering continues from
`0008_memory_origin` (the last file in the parent directory) rather than restarting at
`0001`, so the two directories still read as one project history even though they are
migrated independently.

Everything else matches `ars_memory.db`: `NNNN_name.up.sql` / `.down.sql` pairs, a
ledger table (`_ars_medical_migrations`, so two services' ledgers can never collide even
if they ever did share a connection), WAL + foreign_keys + secure_delete on every
connection — secure_delete matters here at least as much as it does for memory, because
`forget()` on a vital reading is deleting a number that can stand in for a diagnosis.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

logger = logging.getLogger("ars_medical.db")


def default_migrations_dir() -> Path:
    """`services/medical/src/ars_medical/db.py` -> repo root -> `data/migrations/medical`."""
    return Path(__file__).resolve().parents[4] / "data" / "migrations" / "medical"


@dataclass(frozen=True)
class Migration:
    seq: str
    name: str
    up_sql: str
    down_sql: str

    @property
    def id(self) -> str:
        return f"{self.seq}_{self.name}"


def load_migrations(migrations_dir: Path) -> list[Migration]:
    up_files = sorted(migrations_dir.glob("*.up.sql"))
    migrations: list[Migration] = []
    for up_file in up_files:
        base_name = up_file.name.removesuffix(".up.sql")
        seq = base_name.split("_", 1)[0]
        name = base_name.split("_", 1)[1] if "_" in base_name else base_name
        down_file = up_file.with_name(f"{base_name}.down.sql")
        down_sql = down_file.read_text() if down_file.exists() else ""
        migrations.append(
            Migration(seq=seq, name=name, up_sql=up_file.read_text(), down_sql=down_sql)
        )
    return migrations


_LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS _ars_medical_migrations (
    id TEXT PRIMARY KEY,
    applied_at_ms INTEGER NOT NULL
);
"""


async def migrate(conn: aiosqlite.Connection, migrations_dir: Path | None = None) -> list[str]:
    """Applies every migration not yet in the ledger, in order. Returns the ids applied
    this call (for tests/telemetry)."""
    migrations_dir = migrations_dir or default_migrations_dir()
    await conn.executescript(_LEDGER_SQL)
    cursor = await conn.execute("SELECT id FROM _ars_medical_migrations")
    applied = {row[0] for row in await cursor.fetchall()}

    newly_applied: list[str] = []
    for migration in load_migrations(migrations_dir):
        if migration.id in applied:
            continue
        await conn.executescript(migration.up_sql)
        await conn.execute(
            "INSERT INTO _ars_medical_migrations (id, applied_at_ms) VALUES (?, ?)",
            (migration.id, int(time.time() * 1000)),
        )
        await conn.commit()
        newly_applied.append(migration.id)
    return newly_applied


async def open_connection(db_path: Path) -> aiosqlite.Connection:
    """Opens (creating parent dirs as needed), enables WAL + foreign keys + secure
    delete. Does NOT run migrations — call `migrate()` explicitly so callers control
    when schema changes happen, same contract as `ars_memory.db.open_connection`."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    # Overwrite deleted content with zeroes instead of leaving it in free pages. Without
    # this, `forget()` unlinks a row but the plaintext value stays in the file and is
    # trivially recoverable with `strings` — for a store whose whole purpose is holding
    # SENSITIVE health data, "the user asked me to delete it" has to mean the bytes are
    # gone, not just unreachable through this store's own queries.
    await conn.execute("PRAGMA secure_delete=ON")
    return conn


async def wal_checkpoint_truncate(conn: aiosqlite.Connection) -> None:
    """`secure_delete` scrubs the main database file, but the original INSERT frames
    still sit in the write-ahead log until it is checkpointed — deleted values survive
    in `medical.db-wal`, which is exactly where a forensic scan looks. Truncate the WAL
    so a deletion is real on disk, not just real in the query surface. Off the hot
    path: `forget()` is rare and user-initiated, same as `ars_memory`."""
    await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
