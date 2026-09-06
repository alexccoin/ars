"""Configuration for `services/memory`. Same convention as `ars_core.config`: every
value has a local-first default, nothing requires network or an account to boot."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MemoryConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARS_MEMORY_", env_file=".env", extra="ignore")

    data_dir: Path = Path("./var")
    db_filename: str = "memory.db"

    embedding_backend: str = "sentence_transformer"
    """Real multilingual embeddings by default. The alternative, "hash", is a test
    double: it matches on shared substrings, so "cât este chiria" would never find an
    English lease. A.R.S is bilingual and the document tier is only as good as this, so
    the real backend is the default and the first run downloads weights (~120 MB)."""

    sentence_transformer_model: str = "intfloat/multilingual-e5-small"
    """Chosen by measurement — `research/benchmarks/retrieval_calibration.py` — because
    it is the one whose relevant and irrelevant scores actually separate, in Romanian as
    well as English. Changing this invalidates every stored vector; the store notices and
    re-embeds. Re-run the benchmark and update the constants in the gateway's brain."""

    # --- hybrid ranking weights (see ars_memory.ranking for the formula) ---
    rank_weight_vector: float = 0.55
    rank_weight_keyword: float = 0.35
    rank_weight_recency: float = 0.10
    rank_recency_half_life_days: float = 90.0
    rank_vector_candidates: int = 50
    rank_keyword_candidates: int = 50

    # --- retention (security/policies/retention.md) ---
    retention_personal_superseded_days: int = 180
    retention_public_superseded_days: int = 365
    retention_sensitive_cap_days: int = 90
    retention_sensitive_superseded_days: int = 30

    db_path_override: Path | None = None
    """Set this to point the store at an exact file. `db_path` is a derived property,
    so passing `db_path=` to the constructor would otherwise be silently dropped by
    pydantic and the store would quietly write to the default location instead —
    which, for a database of private memories, is a privacy bug rather than a typo."""

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
