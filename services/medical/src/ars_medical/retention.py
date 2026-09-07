"""Retention sweeper for vital readings — enforces
`security/policies/retention-medical.md`. Read that file first: the policy here is
deliberately asymmetric versus `ars_memory.retention.RetentionSweeper`, and the
reasoning is there, not repeated in code comments.

Like the memory sweeper, this never deletes any way other than the store's own
`forget()` — the sweep and a caller-initiated `forget()` are the same code path, so
"deletion actually deletes" is proven once
(`tests/unit/medical/test_forget_deletes_vitals.py`) and inherited here, not re-tested
from scratch.
"""

from __future__ import annotations

from dataclasses import dataclass

from ars_protocol import now_ms
from ars_telemetry import span

from .config import MedicalConfig
from .store import SqliteMedicalStore

MS_PER_DAY = 86_400_000


@dataclass(frozen=True, slots=True)
class RetentionReport:
    superseded_purged: int
    current_capped: int

    @property
    def total_deleted(self) -> int:
        return self.superseded_purged + self.current_capped


class RetentionSweeper:
    def __init__(self, store: SqliteMedicalStore, config: MedicalConfig) -> None:
        self._store = store
        self._config = config

    async def sweep(self, *, at_ms: int | None = None) -> RetentionReport:
        now = at_ms if at_ms is not None else now_ms()
        with span("medical.retention.sweep"):
            rows = await self._store.readings_for_retention_scan()

            superseded_purge: list[str] = []
            current_cap: list[str] = []

            for row in rows:
                reading_id = row["id"]
                measured_at_ms = row["measured_at_ms"]
                is_superseded = row["superseded_by"] is not None
                age_days = max(0.0, now - measured_at_ms) / MS_PER_DAY

                if is_superseded:
                    if age_days >= self._config.retention_superseded_days:
                        superseded_purge.append(reading_id)
                    # else: within the historical window, kept — see
                    # security/policies/retention-medical.md.
                    continue

                # Live reading: capped only if the caller opted into a cap
                # (retention_current_days is None by default — see config.py and
                # security/policies/retention-medical.md for why that default is not
                # an oversight).
                cap = self._config.retention_current_days
                if cap is not None and age_days >= cap:
                    current_cap.append(reading_id)

            superseded_count = await self._delete_all(superseded_purge)
            capped_count = await self._delete_all(current_cap)

            report = RetentionReport(
                superseded_purged=superseded_count, current_capped=capped_count
            )
            await self._store.record_retention_sweep(
                ran_at_ms=now,
                superseded_purged_count=report.superseded_purged,
                current_capped_count=report.current_capped,
                total_deleted=report.total_deleted,
            )
            return report

    async def _delete_all(self, reading_ids: list[str]) -> int:
        deleted = 0
        for reading_id in reading_ids:
            deleted += await self._store.forget(reading_id=reading_id)
        return deleted


__all__ = ["RetentionReport", "RetentionSweeper"]
