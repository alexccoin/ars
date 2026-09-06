"""Retention sweeper — enforces `security/policies/retention.md` by calling
`SqliteMemoryStore.forget()` for every record whose time is up. It never deletes any
other way: the sweep and a caller-initiated `forget()` are the same code path, so
"deletion actually deletes" is proven once (tests/unit/memory/test_forget_deletes_everything.py)
and inherited here, not re-tested from scratch.
"""

from __future__ import annotations

from dataclasses import dataclass

from ars_protocol import Sensitivity, now_ms
from ars_telemetry import span

from .config import MemoryConfig
from .store import SqliteMemoryStore

MS_PER_DAY = 86_400_000


@dataclass(frozen=True)
class RetentionReport:
    expired_valid_until: int
    superseded_purged: int
    sensitivity_cap: int

    @property
    def total_deleted(self) -> int:
        return self.expired_valid_until + self.superseded_purged + self.sensitivity_cap


class RetentionSweeper:
    def __init__(self, store: SqliteMemoryStore, config: MemoryConfig) -> None:
        self._store = store
        self._config = config

    async def sweep(self, *, at_ms: int | None = None) -> RetentionReport:
        now = at_ms if at_ms is not None else now_ms()
        with span("memory.retention.sweep"):
            rows = await self._store.records_for_retention_scan()

            expired: list[str] = []
            superseded_purge: list[str] = []
            sensitivity_cap: list[str] = []

            for row in rows:
                record_id = row["id"]
                valid_until_ms = row["valid_until_ms"]
                created_at_ms = row["created_at_ms"]
                sensitivity = Sensitivity(row["sensitivity"])
                is_superseded = row["superseded_by"] is not None

                # Rule 1 (retention.md): valid_until_ms is always honored, regardless
                # of sensitivity, and wins over every other rule.
                if valid_until_ms is not None and valid_until_ms <= now:
                    expired.append(record_id)
                    continue

                age_days = max(0.0, now - created_at_ms) / MS_PER_DAY

                if sensitivity is Sensitivity.SENSITIVE:
                    cap = (
                        self._config.retention_sensitive_superseded_days
                        if is_superseded
                        else self._config.retention_sensitive_cap_days
                    )
                    if age_days >= cap:
                        sensitivity_cap.append(record_id)
                        continue
                elif is_superseded:
                    cap = (
                        self._config.retention_public_superseded_days
                        if sensitivity is Sensitivity.PUBLIC
                        else self._config.retention_personal_superseded_days
                    )
                    if age_days >= cap:
                        superseded_purge.append(record_id)
                        continue
                # else: current PUBLIC/PERSONAL record with no valid_until_ms — durable
                # by design (security/policies/retention.md), not touched here.

            expired_count = await self._delete_all(expired)
            superseded_count = await self._delete_all(superseded_purge)
            cap_count = await self._delete_all(sensitivity_cap)

            report = RetentionReport(
                expired_valid_until=expired_count,
                superseded_purged=superseded_count,
                sensitivity_cap=cap_count,
            )
            await self._store.record_retention_sweep(
                ran_at_ms=now,
                expired_valid_until_count=report.expired_valid_until,
                superseded_purged_count=report.superseded_purged,
                sensitivity_cap_count=report.sensitivity_cap,
                total_deleted=report.total_deleted,
            )
            return report

    async def _delete_all(self, record_ids: list[str]) -> int:
        deleted = 0
        for record_id in record_ids:
            deleted += await self._store.forget(record_id=record_id)
        return deleted
