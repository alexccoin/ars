"""Default reference ranges, seeded on every `open()` (idempotent — see
`ars_medical.store.SqliteMedicalStore._seed_default_ranges`).

Every entry has a real, checkable `source`, per `ReferenceRange`'s docstring in
`packages/protocol/src/ars_protocol/health.py`: "a range with no provenance is an
opinion". These are the "normal" category from a named, mainstream clinical source —
not A.R.S's own judgement of what is healthy, and not the only category that source
publishes (blood pressure in particular has several categories above `high`; this store
only ever cites the one number the user's reading is compared against).

`WEIGHT`, `STEPS` and `SLEEP_MINUTES` have no entry here on purpose. A "normal" weight
depends on height and frame in a way a single mmHg-style range does not, and step/sleep
targets are population goals, not a clinical reference range — seeding a fixed number
for either would carry the same false authority `ReferenceRange.source` exists to
prevent, just with the citation pointing nowhere. A caller with a real, cited source for
one of these (e.g. a clinician's individualised target) can still add it via
`SqliteMedicalStore.add_range`.
"""

from __future__ import annotations

from ars_protocol import ReferenceRange, VitalKind

DEFAULT_REFERENCE_RANGES: tuple[ReferenceRange, ...] = (
    ReferenceRange(
        kind=VitalKind.HEART_RATE,
        low=60.0,
        high=100.0,
        source="American Heart Association",
        note="Normal resting heart rate for adults, 60-100 bpm.",
    ),
    ReferenceRange(
        kind=VitalKind.BLOOD_PRESSURE_SYSTOLIC,
        low=90.0,
        high=120.0,
        source="American Heart Association / American College of Cardiology, "
        "2017 Hypertension Guideline",
        note="'Normal' category; the guideline defines further categories above 120.",
    ),
    ReferenceRange(
        kind=VitalKind.BLOOD_PRESSURE_DIASTOLIC,
        low=60.0,
        high=80.0,
        source="American Heart Association / American College of Cardiology, "
        "2017 Hypertension Guideline",
        note="'Normal' category; the guideline defines further categories above 80.",
    ),
    ReferenceRange(
        kind=VitalKind.SPO2,
        low=95.0,
        high=100.0,
        source="American Thoracic Society",
        note="Normal oxygen saturation for a healthy adult at sea level.",
    ),
    ReferenceRange(
        kind=VitalKind.BODY_TEMPERATURE,
        low=36.1,
        high=37.2,
        source="Mayo Clinic",
        note="Normal adult body temperature range (36.1-37.2 degC / 97-99 degF).",
    ),
    ReferenceRange(
        kind=VitalKind.BLOOD_GLUCOSE,
        low=3.9,
        high=5.6,
        source="American Diabetes Association",
        note="Normal fasting plasma glucose, 3.9-5.6 mmol/L (70-100 mg/dL). Only "
        "meaningful for a fasting reading; this store has no way to know if one was.",
    ),
    ReferenceRange(
        kind=VitalKind.RESPIRATORY_RATE,
        low=12.0,
        high=20.0,
        source="MedlinePlus (U.S. National Library of Medicine)",
        note="Normal adult resting respiratory rate, 12-20 breaths/min.",
    ),
)

__all__ = ["DEFAULT_REFERENCE_RANGES"]
