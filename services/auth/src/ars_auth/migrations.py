"""Locating and applying the SQL in ``services/auth/data/migrations/<component>``.

One source of truth for the schema: the ``.sql`` files. They are the reviewable artefact
- a security reviewer should be able to read the schema of the consent record without
reading Python - and they are copied into the wheel at build time so an installed service
can bootstrap itself without the repo present.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import aiosqlite
from ars_protocol import now_ms

__all__ = ["apply_migrations", "migrations_dir"]


def migrations_dir(component: str) -> Path:
    """Directory of ``.sql`` files for one component ("grants", "vault").

    Resolved two ways: the copy bundled into the wheel for a normal install, and the repo
    path for an editable install.
    """
    try:
        bundled = Path(str(resources.files("ars_auth"))) / "_bundled" / "migrations" / component
        if bundled.is_dir() and any(bundled.glob("*.sql")):
            return bundled
    except (ModuleNotFoundError, TypeError, NotImplementedError):  # pragma: no cover
        pass
    repo = Path(__file__).resolve().parents[2] / "data" / "migrations" / component
    if repo.is_dir():
        return repo
    raise FileNotFoundError(
        f"ars_auth migrations for '{component}' not found "
        "(looked in the wheel bundle and in the repo tree)"
    )


async def apply_migrations(db: aiosqlite.Connection, component: str) -> tuple[str, ...]:
    """Apply every unapplied ``.sql`` file in order. Returns the ones applied now.

    Idempotent, and recorded in ``schema_migrations`` so a partially-migrated database
    does not re-run statements that already succeeded.
    """
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  version TEXT PRIMARY KEY, applied_at_ms INTEGER NOT NULL)"
    )
    await db.commit()
    async with db.execute("SELECT version FROM schema_migrations") as cur:
        applied = {row[0] for row in await cur.fetchall()}

    ran: list[str] = []
    for path in sorted(migrations_dir(component).glob("*.sql")):
        if path.name in applied:
            continue
        await db.executescript(path.read_text(encoding="utf-8"))
        await db.execute(
            "INSERT INTO schema_migrations (version, applied_at_ms) VALUES (?, ?)",
            (path.name, now_ms()),
        )
        await db.commit()
        ran.append(path.name)
    return tuple(ran)
