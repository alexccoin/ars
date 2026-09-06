"""Connection management + schema migration for the memory database.

Migrations live at `data/migrations/` (repo root — see `packages/core` interface
docstring for `MemoryStore`), as numbered `NNNN_name.up.sql` / `NNNN_name.down.sql`
pairs. One file, `0004_memory_vec.optional.*.sql`, is marked OPTIONAL: it creates the
sqlite-vec `vec0` virtual table and is only expected to succeed on a SQLite build where
extension loading is compiled in. This service probed that on the target hardware
(Apple Silicon / arm64, this repo's `.venv`) and it works — see `ars_memory.store`
module docstring for the result — but the optional/fallback machinery stays in place
for any machine where it does not.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

logger = logging.getLogger("ars_memory.db")


def default_migrations_dir() -> Path:
    """`services/memory/src/ars_memory/db.py` -> repo root -> `data/migrations`."""
    return Path(__file__).resolve().parents[4] / "data" / "migrations"


@dataclass(frozen=True)
class Migration:
    seq: str
    name: str
    optional: bool
    up_sql: str
    down_sql: str

    @property
    def id(self) -> str:
        return f"{self.seq}_{self.name}"


def load_migrations(migrations_dir: Path) -> list[Migration]:
    up_files = sorted(migrations_dir.glob("*.up.sql"))
    migrations: list[Migration] = []
    for up_file in up_files:
        raw_name = up_file.name.removesuffix(".up.sql")
        optional = raw_name.endswith(".optional")
        base_name = raw_name.removesuffix(".optional")
        seq = base_name.split("_", 1)[0]
        name = base_name.split("_", 1)[1] if "_" in base_name else base_name
        down_suffix = ".optional.down.sql" if optional else ".down.sql"
        down_file = up_file.with_name(f"{base_name}{down_suffix}")
        down_sql = down_file.read_text() if down_file.exists() else ""
        migrations.append(
            Migration(
                seq=seq,
                name=name,
                optional=optional,
                up_sql=up_file.read_text(),
                down_sql=down_sql,
            )
        )
    return migrations


_LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS _ars_memory_migrations (
    id TEXT PRIMARY KEY,
    applied_at_ms INTEGER NOT NULL
);
"""


async def migrate(conn: aiosqlite.Connection, migrations_dir: Path | None = None) -> list[str]:
    """Applies every migration not yet in the ledger, in order.

    Optional migrations that fail (missing sqlite-vec module) are logged and left out
    of the ledger, so a future connection — after the extension becomes available — will
    retry them automatically. Returns the list of migration ids actually applied this
    call (for tests/telemetry).
    """
    migrations_dir = migrations_dir or default_migrations_dir()
    await conn.executescript(_LEDGER_SQL)
    cursor = await conn.execute("SELECT id FROM _ars_memory_migrations")
    applied = {row[0] for row in await cursor.fetchall()}

    newly_applied: list[str] = []
    for migration in load_migrations(migrations_dir):
        if migration.id in applied:
            continue
        try:
            await conn.executescript(migration.up_sql)
        except sqlite3.OperationalError as exc:
            if migration.optional:
                logger.warning(
                    "optional migration %s skipped (falling back): %s", migration.id, exc
                )
                await conn.rollback()
                continue
            raise
        await conn.execute(
            "INSERT INTO _ars_memory_migrations (id, applied_at_ms) VALUES (?, ?)",
            (migration.id, int(time.time() * 1000)),
        )
        await conn.commit()
        newly_applied.append(migration.id)
    return newly_applied


async def sqlite_vec_available(conn: aiosqlite.Connection) -> bool:
    """Best-effort: try to load the sqlite-vec extension on this connection. SQLite
    loadable extensions are per-connection, not persisted in the file, so this must run
    every time a connection is opened, not just once at migration time."""
    try:
        import sqlite_vec
    except ImportError:
        logger.info("sqlite-vec package not installed; using numpy brute-force index")
        return False
    try:
        await conn.enable_load_extension(True)
        await conn.load_extension(sqlite_vec.loadable_path())
        await conn.enable_load_extension(False)
        return True
    except (sqlite3.OperationalError, AttributeError) as exc:
        logger.warning(
            "sqlite-vec extension failed to load (%s); using numpy brute-force index", exc
        )
        return False


async def open_connection(db_path: Path) -> tuple[aiosqlite.Connection, bool]:
    """Opens (creating parent dirs as needed), enables WAL + foreign keys, probes
    sqlite-vec, and returns `(connection, vec_available)`. Does NOT run migrations —
    call `migrate()` explicitly so callers control when schema changes happen."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    # Overwrite deleted content with zeroes instead of leaving it in free pages.
    # Without this, `forget()` unlinks a row but the plaintext stays in the file and
    # is trivially recoverable with `strings`. For a store that holds SENSITIVE
    # records, "the user asked me to delete it" has to mean the bytes are gone.
    await conn.execute("PRAGMA secure_delete=ON")
    vec_available = await sqlite_vec_available(conn)
    return conn, vec_available
