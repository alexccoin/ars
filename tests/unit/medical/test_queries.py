"""Series, latest-per-kind and aggregates — the whole product argument for this store,
per the task brief: "what was my blood pressure last week" must answer from the CPU
tier and never wake a 14B model. These tests are about correctness of the numbers, not
speed (see tests/load/test_medical_query_budget.py for the budget itself).
"""

from __future__ import annotations

from ars_medical import Trend
from ars_protocol import VitalKind, VitalReading

DAY_MS = 24 * 60 * 60 * 1000


async def test_series_returns_readings_within_the_window_ordered_by_time(store):
    base = 1_700_000_000_000
    in_window = [
        await store.record(
            VitalReading(
                kind=VitalKind.HEART_RATE, value=float(70 + i), measured_at_ms=base + i * 1000
            )
        )
        for i in range(5)
    ]
    before = await store.record(
        VitalReading(kind=VitalKind.HEART_RATE, value=60.0, measured_at_ms=base - DAY_MS)
    )
    after = await store.record(
        VitalReading(kind=VitalKind.HEART_RATE, value=90.0, measured_at_ms=base + DAY_MS)
    )

    result = await store.series(VitalKind.HEART_RATE, since_ms=base, until_ms=base + 4000)

    assert [r.id for r in result] == [r.id for r in in_window]
    assert before.id not in {r.id for r in result}
    assert after.id not in {r.id for r in result}
    assert [r.measured_at_ms for r in result] == sorted(r.measured_at_ms for r in result), (
        "series must be chronological — a caller charting this expects it pre-sorted"
    )


async def test_series_excludes_superseded_readings_by_default(store):
    old = await store.record(VitalReading(kind=VitalKind.WEIGHT, value=95.0))
    new = await store.record(VitalReading(kind=VitalKind.WEIGHT, value=94.5))
    await store.record(old.model_copy(update={"superseded_by": new.id}))

    result = await store.series(VitalKind.WEIGHT)

    assert [r.id for r in result] == [new.id]


async def test_latest_returns_none_for_a_kind_with_no_data(store):
    assert await store.latest(VitalKind.BLOOD_GLUCOSE) is None


async def test_latest_all_returns_the_most_recent_reading_per_kind(store):
    base = 1_700_000_000_000
    await store.record(VitalReading(kind=VitalKind.HEART_RATE, value=70.0, measured_at_ms=base))
    newest_hr = await store.record(
        VitalReading(kind=VitalKind.HEART_RATE, value=75.0, measured_at_ms=base + 1000)
    )
    only_spo2 = await store.record(
        VitalReading(kind=VitalKind.SPO2, value=98.0, measured_at_ms=base)
    )

    latest = await store.latest_all()
    by_kind = {r.kind: r for r in latest}

    assert by_kind[VitalKind.HEART_RATE].id == newest_hr.id
    assert by_kind[VitalKind.SPO2].id == only_spo2.id
    assert VitalKind.BLOOD_GLUCOSE not in by_kind, "a kind with no readings must not appear at all"


async def test_aggregate_computes_mean_min_max_count_over_the_window(store):
    base = 1_700_000_000_000
    values = [60.0, 70.0, 80.0, 90.0]
    for i, value in enumerate(values):
        await store.record(
            VitalReading(kind=VitalKind.HEART_RATE, value=value, measured_at_ms=base + i * 1000)
        )
    # Outside the window entirely — must not pollute the aggregate.
    await store.record(
        VitalReading(kind=VitalKind.HEART_RATE, value=200.0, measured_at_ms=base - DAY_MS)
    )

    agg = await store.aggregate(VitalKind.HEART_RATE, since_ms=base, until_ms=base + 3000)

    assert agg.count == 4
    assert agg.mean == 75.0
    assert agg.minimum == 60.0
    assert agg.maximum == 90.0


async def test_aggregate_with_no_readings_in_window_is_all_none_not_zero(store):
    """`count == 0` with `mean == 0.0` would read as "average heart rate zero", which is
    a different and false claim from "no readings in this window". None must mean
    "nothing to average", never a number."""
    agg = await store.aggregate(VitalKind.HEART_RATE, since_ms=0, until_ms=1)

    assert agg.count == 0
    assert agg.mean is None
    assert agg.minimum is None
    assert agg.maximum is None
    assert agg.trend == Trend.UNKNOWN


async def test_trend_reports_rising_when_the_second_half_of_the_window_is_higher(store):
    base = 1_700_000_000_000
    window = 10 * DAY_MS
    # First half: steady around 60. Second half: steady around 90 — an unambiguous rise.
    for i in range(3):
        await store.record(
            VitalReading(kind=VitalKind.HEART_RATE, value=60.0, measured_at_ms=base + i * DAY_MS)
        )
    for i in range(7, 10):
        await store.record(
            VitalReading(kind=VitalKind.HEART_RATE, value=90.0, measured_at_ms=base + i * DAY_MS)
        )

    agg = await store.aggregate(VitalKind.HEART_RATE, since_ms=base, until_ms=base + window)

    assert agg.trend == Trend.RISING


async def test_trend_reports_stable_for_noise_within_the_threshold(store):
    """Trend must not flap on ordinary measurement noise — two readings of 72 and 73
    bpm are the same heart rate as far as a trend direction is concerned."""
    base = 1_700_000_000_000
    window = 10 * DAY_MS
    for i, value in [(0, 72.0), (1, 73.0), (8, 72.5), (9, 73.5)]:
        await store.record(
            VitalReading(kind=VitalKind.HEART_RATE, value=value, measured_at_ms=base + i * DAY_MS)
        )

    agg = await store.aggregate(VitalKind.HEART_RATE, since_ms=base, until_ms=base + window)

    assert agg.trend == Trend.STABLE


async def test_trend_is_unknown_with_fewer_than_two_data_points(store):
    await store.record(VitalReading(kind=VitalKind.HEART_RATE, value=72.0))

    agg = await store.aggregate(VitalKind.HEART_RATE, since_ms=0)

    assert agg.trend == Trend.UNKNOWN
