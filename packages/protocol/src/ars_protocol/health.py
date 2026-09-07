"""Health data: what A.R.S may hold about a body, and what it may say about it.

The single source of truth for the medical subsystem, per CLAUDE.md rule 1.

Two decisions are made here rather than downstream, because downstream is too late.

**Everything in this module defaults to `Sensitivity.SENSITIVE`.** Not "personal" —
sensitive. A blood-pressure series is enough to infer a diagnosis, a medication list is
enough to infer one exactly, and `Router._guard_cloud` already refuses to send SENSITIVE
findings to any backend that does not run locally. Making that the default here means the
privacy property holds for code nobody has written yet.

**Nothing in this module can express a diagnosis.** There is a reading, there is a
reference range, and there is an observation that a reading sits outside it. There is no
`Condition`, no `Assessment`, no `Recommendation` — because a type is an invitation, and a
field called `diagnosis` would be filled in eventually by a 14B model with no licence and
no examination. What A.R.S is allowed to say is: this is your number, this is the range
your reference gives, and this one is outside it.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import Field

from .common import Model, new_id, now_ms
from .memory import Sensitivity


class VitalKind(StrEnum):
    """What a personal medical device can measure.

    Deliberately closed. Each value here means a unit, a plausible range and a device
    profile have been decided; an open string would mean "whatever the parser happened to
    emit", and a chart of `bp` mixed with `blood_pressure` is a chart of nothing.
    """

    HEART_RATE = "heart_rate"                    # bpm
    BLOOD_PRESSURE_SYSTOLIC = "bp_systolic"      # mmHg
    BLOOD_PRESSURE_DIASTOLIC = "bp_diastolic"    # mmHg
    SPO2 = "spo2"                                # %
    BODY_TEMPERATURE = "body_temperature"        # °C
    BLOOD_GLUCOSE = "blood_glucose"              # mmol/L
    WEIGHT = "weight"                            # kg
    RESPIRATORY_RATE = "respiratory_rate"        # breaths/min
    STEPS = "steps"                              # count
    SLEEP_MINUTES = "sleep_minutes"              # minutes

    @property
    def unit(self) -> str:
        return _UNITS[self]

    @property
    def display_name(self) -> str:
        return self.value.replace("_", " ")


_UNITS: dict[VitalKind, str] = {
    VitalKind.HEART_RATE: "bpm",
    VitalKind.BLOOD_PRESSURE_SYSTOLIC: "mmHg",
    VitalKind.BLOOD_PRESSURE_DIASTOLIC: "mmHg",
    VitalKind.SPO2: "%",
    VitalKind.BODY_TEMPERATURE: "°C",
    VitalKind.BLOOD_GLUCOSE: "mmol/L",
    VitalKind.WEIGHT: "kg",
    VitalKind.RESPIRATORY_RATE: "breaths/min",
    VitalKind.STEPS: "steps",
    VitalKind.SLEEP_MINUTES: "min",
}

PLAUSIBLE: dict[VitalKind, tuple[float, float]] = {
    VitalKind.HEART_RATE: (20.0, 250.0),
    VitalKind.BLOOD_PRESSURE_SYSTOLIC: (50.0, 260.0),
    VitalKind.BLOOD_PRESSURE_DIASTOLIC: (30.0, 160.0),
    VitalKind.SPO2: (50.0, 100.0),
    VitalKind.BODY_TEMPERATURE: (30.0, 45.0),
    VitalKind.BLOOD_GLUCOSE: (1.0, 40.0),
    VitalKind.WEIGHT: (2.0, 400.0),
    VitalKind.RESPIRATORY_RATE: (4.0, 60.0),
    VitalKind.STEPS: (0.0, 200_000.0),
    VitalKind.SLEEP_MINUTES: (0.0, 1440.0),
}
"""What a working device can physically report — NOT what is healthy.

A cuff that slipped reports 20/10; a pulse oximeter on a cold finger reports 40%. Those
are device errors and storing them silently corrupts every average computed afterwards.
This range rejects impossible readings, and only impossible ones: a systolic of 200 is
plausible, alarming, and must be stored exactly as measured. Filtering by what is
*healthy* would delete precisely the readings that matter most.
"""


class DeviceKind(StrEnum):
    BLOOD_PRESSURE_MONITOR = "blood_pressure_monitor"
    PULSE_OXIMETER = "pulse_oximeter"
    THERMOMETER = "thermometer"
    GLUCOMETER = "glucometer"
    SCALE = "scale"
    HEART_RATE_MONITOR = "heart_rate_monitor"
    MANUAL = "manual"
    """Typed in by hand. Not a lesser source — a reading from a clinic's machine arrives
    this way, and it is often the most reliable number in the store."""


class ReadingSource(Model):
    """Where a number came from, in enough detail to distrust it later."""

    device_kind: DeviceKind = DeviceKind.MANUAL
    device_name: str | None = None
    device_id: str | None = None
    """An identifier for the device, scoped to the machine that recorded the reading.

    This said "stable per physical device" until someone checked. On CoreBluetooth the
    only identifier available without pairing is a peripheral UUID that macOS generates
    PER MAC, so the same cuff read from a laptop, a phone and a tablet produces three
    different ids — and "my old cuff read 10 mmHg high" stops being expressible across
    devices. Worse, that is not retrofittable: once readings are written under per-machine
    ids there is nothing left to join them on.

    Left as-is deliberately rather than invented: a genuinely stable id needs the Device
    Information Service's serial number, which requires connecting and which not every
    device exposes. Until then this is a hint, and `device_name` is what a person should
    be shown."""


class VitalReading(Model):
    """One measurement, at one moment.

    Immutable, like every other record in A.R.S: a wrong reading is superseded, never
    edited, because "my blood pressure was 180 last Tuesday" and "it was recorded as 180
    last Tuesday and corrected" are different facts and a clinician needs the second one.
    """

    id: Annotated[str, Field(default_factory=lambda: new_id("vit"))]
    kind: VitalKind
    value: float
    measured_at_ms: int = Field(default_factory=now_ms)
    source: ReadingSource = Field(default_factory=ReadingSource)

    note: str | None = None
    """The user's own words about this reading — "after coffee", "before the run".

    Only ever the user's. A device decoder must NOT write English status text here:
    "irregular pulse detected" arriving from a cuff would be a user-facing string that
    exists in one language, which rule 5 forbids, and it would be indistinguishable from
    something the user typed. Device status belongs in `status`, which is structured and
    rendered in the reader's language at presentation."""

    status: tuple[str, ...] = ()
    """Structured flags the device reported alongside the value, as stable identifiers —
    "irregular_pulse", "cuff_too_loose", "body_movement". Never prose, never translated
    at this layer; the interface turns these into sentences in whichever language it is
    speaking."""
    sensitivity: Sensitivity = Sensitivity.SENSITIVE
    superseded_by: str | None = None

    @property
    def unit(self) -> str:
        return self.kind.unit

    @property
    def is_plausible(self) -> bool:
        low, high = PLAUSIBLE[self.kind]
        return low <= self.value <= high


class ReferenceRange(Model):
    """A range from a named source, for one measurement.

    `source` is required and has no default on purpose. A range with no provenance is an
    opinion, and an opinion about someone's blood pressure delivered by a machine is the
    thing this whole subsystem exists to avoid. If A.R.S says a number is high, it must be
    able to say according to whom.
    """

    kind: VitalKind
    low: float | None = None
    high: float | None = None
    source: str
    note: str | None = None

    def contains(self, value: float) -> bool:
        if self.low is not None and value < self.low:
            return False
        return not (self.high is not None and value > self.high)


class RangeFinding(Model):
    """A reading, and the fact that it sits outside a named range. Not a diagnosis.

    The wording is the point. "Outside the range your reference gives" is a measurement
    compared to a citation, which A.R.S can support. "High blood pressure" is a clinical
    judgement about a person, which it cannot.
    """

    reading: VitalReading
    range: ReferenceRange
    direction: str
    """"below" or "above". Present so the interface never has to infer it from numbers."""


__all__ = [
    "PLAUSIBLE",
    "DeviceKind",
    "RangeFinding",
    "ReadingSource",
    "ReferenceRange",
    "VitalKind",
    "VitalReading",
]
