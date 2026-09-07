"""The budget behind the whole point of this store, asserted.

CLAUDE.md: "Latency is a test." The architecture doc's tier ladder
(docs/architecture/overview.md, "The tier ladder") does not yet have a row for the
medical store specifically — `services/medical` did not exist when that table was
written, and adding an unreviewed number to a document explicitly owned by
`system-architect` is not this change's call to make (see the report for this task).
What is asserted here instead is the number the task brief's own argument requires:
"'what was my blood pressure last week' never wakes a 14B model" is only true if
answering it is cheap enough that nothing would ever be tempted to escalate past it. The
budget below is set by analogy to tier 1 in that ladder (`DOCUMENTS`, "~10 ms, CPU
only") with headroom, not measured against a documented target — flagged as a
recommendation for `system-architect` to turn into a real row, the same way ADR 0001
turned a measured number into the endpointing budget.

Every query here is answered from SQL alone (`COUNT`/`AVG`/`MIN`/`MAX`, or an indexed
range scan, or one window-function pass) — no row is ever pulled into Python to be
reduced by hand — which is what should make this budget hold regardless of how many
readings accumulate, not just at the sample size measured here.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from ars_medical import MedicalConfig, SqliteMedicalStore
from ars_protocol import VitalKind

# Analogy to tier 1 (~10 ms, CPU only) in docs/architecture/overview.md's tier ladder,
# with headroom for a query pattern that has not been measured on real hardware the way
# the tier-1 embedding search has (research/benchmarks/retrieval_calibration.py). Not an
# authoritative number — see this file's module docstring.
BUDGET_P95_MS = 15.0

KINDS = (
    VitalKind.HEART_RATE,
    VitalKind.BLOOD_PRESSURE_SYSTOLIC,
    VitalKind.BLOOD_PRESSURE_DIASTOLIC,
    VitalKind.SPO2,
    VitalKind.WEIGHT,
)
READINGS_PER_KIND = 2000
"""~5.5 years of one reading a day per kind, or a few months of a wearable sampling
several times a day — either way, more than a real single-user store is likely to hold
for any one kind before `security/policies/retention.md`-style aging would apply."""

START_MS = 1_600_000_000_000
STEP_MS = 60 * 60 * 1000  # one reading per kind per hour


async def _seed(store: SqliteMedicalStore) -> None:
    """Bypasses `record()` for setup speed only — this loads ~10k rows through one
    executemany + one commit rather than ~10k individual locked calls, so the *test
    setup* is not what is being measured. `record()`'s own correctness (dedup,
    supersession, rejection) is covered in tests/unit/medical, not here.
    """
    rows = []
    for kind in KINDS:
        for i in range(READINGS_PER_KIND):
            rows.append(
                (
                    f"vit_load_{kind.value}_{i:06d}",
                    kind.value,
                    float(60 + (i % 40)),
                    START_MS + i * STEP_MS,
                    "manual",
                    None,
                    None,
                    None,
                    "sensitive",
                    None,
                )
            )
    await store._conn.executemany(
        """
        INSERT INTO vital_readings (
            id, kind, value, measured_at_ms, device_kind, device_name, device_id,
            note, sensitivity, superseded_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    await store._conn.commit()


def _p95(samples_ms: list[float]) -> float:
    ordered = sorted(samples_ms)
    index = min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))
    return ordered[index]


@pytest.fixture(scope="module")
async def seeded_store(tmp_path_factory: pytest.TempPathFactory):
    tmp_path: Path = tmp_path_factory.mktemp("medical_load")
    config = MedicalConfig(data_dir=tmp_path)
    store = await SqliteMedicalStore.open(config)
    await _seed(store)
    try:
        yield store
    finally:
        await store.close()


async def _timed_runs(coro_factory, *, runs: int = 30) -> list[float]:
    samples: list[float] = []
    for _ in range(runs):
        start = time.perf_counter()
        await coro_factory()
        samples.append((time.perf_counter() - start) * 1000)
    return samples


async def test_series_over_a_week_stays_within_budget(seeded_store):
    """"What was my blood pressure last week" — the exact question from the task
    brief. `since_ms`/`until_ms` bound to one week out of ~2000 hourly rows."""
    since_ms = START_MS + 1000 * STEP_MS
    until_ms = since_ms + 7 * 24 * 60 * 60 * 1000

    samples = await _timed_runs(
        lambda: seeded_store.series(
            VitalKind.BLOOD_PRESSURE_SYSTOLIC, since_ms=since_ms, until_ms=until_ms
        )
    )

    assert _p95(samples) < BUDGET_P95_MS, f"series p95 {_p95(samples):.2f} ms"


async def test_aggregate_over_a_month_stays_within_budget(seeded_store):
    since_ms = START_MS + 1000 * STEP_MS
    until_ms = since_ms + 30 * 24 * 60 * 60 * 1000

    samples = await _timed_runs(
        lambda: seeded_store.aggregate(VitalKind.HEART_RATE, since_ms=since_ms, until_ms=until_ms)
    )

    assert _p95(samples) < BUDGET_P95_MS, f"aggregate p95 {_p95(samples):.2f} ms"


async def test_latest_stays_within_budget(seeded_store):
    samples = await _timed_runs(lambda: seeded_store.latest(VitalKind.SPO2))

    assert _p95(samples) < BUDGET_P95_MS, f"latest p95 {_p95(samples):.2f} ms"


async def test_latest_all_stays_within_budget(seeded_store):
    """The most expensive read this store offers — a window function over every kind at
    once — is the one a "how am I doing" turn would actually call."""
    samples = await _timed_runs(lambda: seeded_store.latest_all())

    assert _p95(samples) < BUDGET_P95_MS, f"latest_all p95 {_p95(samples):.2f} ms"
