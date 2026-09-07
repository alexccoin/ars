"""Configuration for `services/medical`. Same convention as `ars_memory.config`: every
value has a local-first default, nothing requires network or an account to boot."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MedicalConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARS_MEDICAL_", env_file=".env", extra="ignore")

    data_dir: Path = Path("./var")
    db_filename: str = "medical.db"

    seed_default_ranges: bool = True
    """Load `ars_medical.ranges.DEFAULT_REFERENCE_RANGES` on every `open()`. Idempotent
    — the store upserts by `(kind, source)`, so calling this on every start is safe and
    is what makes "findings" work out of the box without a separate setup step. Set
    False only for a test that wants an empty range table to assert against."""

    # --- retention (security/policies/retention-medical.md) ---
    retention_superseded_days: int = 30
    """A superseded reading (`superseded_by IS NOT NULL` — a correction was recorded)
    is purged this many days after `measured_at_ms`. Deliberately as tight as the
    matching `SENSITIVE` window in `ars_memory` (`retention_sensitive_superseded_days`,
    security/policies/retention.md) rather than looser: once a value has been
    corrected, what remains is an audit trail of the correction, not the fact itself,
    and this is health data — CLAUDE.md rule 7 "applies with more force" here than
    anywhere else in the system."""

    retention_current_days: int | None = None
    """A *live* (non-superseded) reading is purged this many days after
    `measured_at_ms` — `None` (the default) means never, by the sweeper, on its own.

    This is the one deliberate divergence from `ars_memory`'s policy, and it is a
    decision, not an oversight: see `security/policies/retention-medical.md` for the
    full reasoning. In short, `ars_memory`'s hard `SENSITIVE` cap exists to stop silent
    accumulation of a fact that has gone stale, with the caller expected to
    "re-remember" it if it is still true — a vital reading has no equivalent
    "still true" to re-affirm; it is a historical measurement of a specific moment, and
    the entire reason `SqliteMedicalStore.series`/`aggregate` exist is to answer "how
    has this trended", a question a rolling deletion window actively destroys the
    answer to. A caller (e.g. a future user preference, "delete my vitals after a
    year") can still set this to an explicit number; the sweeper will honour it."""

    db_path_override: Path | None = None
    """Set this to point the store at an exact file. `db_path` is a derived property,
    so passing `db_path=` to the constructor would otherwise be silently dropped by
    pydantic and the store would quietly write to the default location instead — for a
    database of health data, that is a privacy bug rather than a typo. Same guard as
    `ars_memory.config.MemoryConfig`."""

    @model_validator(mode="before")
    @classmethod
    def _reject_silently_ignored_db_path(cls, data: Any) -> Any:
        if isinstance(data, dict) and "db_path" in data:
            raise ValueError(
                "db_path is derived from data_dir/db_filename and cannot be set "
                "directly; use db_path_override=... (or data_dir=...) instead"
            )
        return data

    @property
    def db_path(self) -> Path:
        return self.db_path_override or (self.data_dir / self.db_filename)
