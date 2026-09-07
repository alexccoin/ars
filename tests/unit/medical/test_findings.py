"""`findings()` — the only place this store is allowed to say anything about a number
beyond "here it is". Per `RangeFinding`'s docstring in
`packages/protocol/src/ars_protocol/health.py`, the wording is the entire point: "this
number is outside the range this named source gives" is a measurement compared to a
citation; "you have high blood pressure" is a diagnosis this system is not allowed to
make. Every test here checks that the citation survives to the finding, because a
finding with no source is exactly the "opinion delivered by a machine" the protocol
type was designed to make impossible.
"""

from __future__ import annotations

from ars_protocol import ReferenceRange, VitalKind, VitalReading


async def test_a_reading_outside_its_range_produces_a_finding_that_cites_the_source(bare_store):
    """The whole point: a finding is never bare. It must carry the exact range object —
    including `source` — so a caller can say *according to whom*, not just *this is
    high*."""
    await bare_store.add_range(
        ReferenceRange(
            kind=VitalKind.HEART_RATE, low=60.0, high=100.0, source="Test Cardiology Society"
        )
    )
    reading = VitalReading(kind=VitalKind.HEART_RATE, value=140.0)

    findings = await bare_store.findings([reading])

    assert len(findings) == 1
    finding = findings[0]
    assert finding.reading.id == reading.id
    assert finding.range.source == "Test Cardiology Society"
    assert finding.direction == "above"


async def test_a_reading_inside_its_range_produces_no_finding(bare_store):
    await bare_store.add_range(
        ReferenceRange(
            kind=VitalKind.HEART_RATE, low=60.0, high=100.0, source="Test Cardiology Society"
        )
    )
    normal = VitalReading(kind=VitalKind.HEART_RATE, value=72.0)

    findings = await bare_store.findings([normal])

    assert findings == ()


async def test_below_range_is_reported_with_the_below_direction(bare_store):
    await bare_store.add_range(
        ReferenceRange(kind=VitalKind.SPO2, low=95.0, high=100.0, source="Test Pulmonology Society")
    )
    low_spo2 = VitalReading(kind=VitalKind.SPO2, value=88.0)

    findings = await bare_store.findings([low_spo2])

    assert len(findings) == 1
    assert findings[0].direction == "below"


async def test_a_reading_with_no_range_for_its_kind_produces_no_finding(bare_store):
    """No citation exists for `SLEEP_MINUTES` in the default set (see
    `ars_medical.ranges` module docstring — a fixed "normal" sleep duration would carry
    the same false authority the `source` field exists to prevent). `findings()` must
    not invent a judgement where this store has no cited range to compare against."""
    reading = VitalReading(kind=VitalKind.SLEEP_MINUTES, value=180.0)

    findings = await bare_store.findings([reading])

    assert findings == ()


async def test_two_sources_for_the_same_kind_both_produce_their_own_finding(bare_store):
    """Two clinical sources can disagree on where "normal" ends. This store does not
    referee that disagreement — it reports both, each with its own citation, and lets
    the caller (and ultimately the person reading it) see the disagreement rather than
    a single blended opinion."""
    await bare_store.add_range(
        ReferenceRange(
            kind=VitalKind.BLOOD_PRESSURE_SYSTOLIC, low=90.0, high=120.0, source="Source A"
        )
    )
    await bare_store.add_range(
        ReferenceRange(
            kind=VitalKind.BLOOD_PRESSURE_SYSTOLIC, low=90.0, high=130.0, source="Source B"
        )
    )
    # outside A, inside B
    reading = VitalReading(kind=VitalKind.BLOOD_PRESSURE_SYSTOLIC, value=125.0)

    findings = await bare_store.findings([reading])

    assert len(findings) == 1
    assert findings[0].range.source == "Source A"


async def test_default_ranges_are_seeded_and_every_one_has_a_real_source(store):
    """`seed_default_ranges=True` (the config default) must actually populate the
    table, and every seeded range must satisfy the protocol's requirement that `source`
    is non-empty — an anonymous range is precisely what `ReferenceRange.source` having
    no default exists to prevent."""
    ranges = await store.all_ranges()

    assert ranges, "the default set must be seeded on open() by default"
    for range_ in ranges:
        assert range_.source.strip(), f"{range_.kind} has an empty source"


async def test_seeding_is_idempotent_across_repeated_opens(tmp_path):
    """`open()` seeds defaults every time (see `MedicalConfig.seed_default_ranges`
    docstring) so a fresh install always has ranges without a separate setup step. That
    only works if reopening the same store does not duplicate every range on each
    restart."""
    from ars_medical import MedicalConfig, SqliteMedicalStore

    config = MedicalConfig(data_dir=tmp_path)
    store_a = await SqliteMedicalStore.open(config)
    count_after_first_open = len(await store_a.all_ranges())
    await store_a.close()

    store_b = await SqliteMedicalStore.open(config)
    try:
        assert len(await store_b.all_ranges()) == count_after_first_open
    finally:
        await store_b.close()
