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
