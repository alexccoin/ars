"""The health contract, and the two things it deliberately cannot do.

Both are decisions that have to hold for code nobody has written yet, which is why they
are asserted against the protocol rather than against any one service.
"""

from __future__ import annotations

import pytest
from ars_protocol import (
    PLAUSIBLE, Capability, ReferenceRange, Sensitivity, VitalKind, VitalReading,
)


def test_every_reading_is_sensitive_by_default() -> None:
    """Not "personal" — sensitive. A blood-pressure series is enough to infer a diagnosis
    and a medication list is enough to infer one exactly, and the router already refuses
    to send SENSITIVE findings to a backend that does not run locally. Defaulting here is
    what makes that hold without every future caller remembering."""
    assert VitalReading(kind=VitalKind.HEART_RATE, value=62).sensitivity is Sensitivity.SENSITIVE


def test_the_protocol_cannot_express_a_diagnosis() -> None:
    """A type is an invitation. A field called `diagnosis` would be filled in eventually,
    by a 14B model with no licence and no examination. What A.R.S may say is: this is your
    number, this is the range your reference gives, and this one is outside it."""
    import ars_protocol

    for forbidden in ("Diagnosis", "Assessment", "Recommendation", "Prescription", "Triage"):
        assert not hasattr(ars_protocol, forbidden), forbidden


def test_a_reference_range_must_name_its_source() -> None:
    """A range with no provenance is an opinion, and an opinion about someone's blood
    pressure delivered by a machine is what this subsystem exists to avoid. If A.R.S says
    a number is high, it must be able to say according to whom."""
    with pytest.raises(Exception):
        ReferenceRange(kind=VitalKind.HEART_RATE, low=60, high=100)  # type: ignore[call-arg]

    cited = ReferenceRange(kind=VitalKind.HEART_RATE, low=60, high=100, source="NICE 2023")
    assert cited.source


@pytest.mark.parametrize("kind,value,plausible", [
    (VitalKind.BLOOD_PRESSURE_SYSTOLIC, 200, True),   # alarming, and real
    (VitalKind.BLOOD_PRESSURE_SYSTOLIC, 20, False),   # the cuff slipped
    (VitalKind.SPO2, 40, False),                      # a cold finger, not a person
    (VitalKind.SPO2, 88, True),                       # low, and worth keeping
    (VitalKind.BODY_TEMPERATURE, 41.2, True),         # a real fever
    (VitalKind.BODY_TEMPERATURE, 12.0, False),        # a sensor error
])
def test_plausibility_rejects_impossible_readings_and_keeps_alarming_ones(
    kind: VitalKind, value: float, plausible: bool
) -> None:
    """The range is what a working device can physically report, NOT what is healthy.
    Filtering by health would delete precisely the readings that matter most."""
    assert VitalReading(kind=kind, value=value).is_plausible is plausible


def test_every_kind_has_a_unit_and_a_plausible_range() -> None:
    """A kind without a unit is a number with no meaning, and a chart of unitless numbers
    is a chart of nothing."""
    for kind in VitalKind:
        assert kind.unit
        assert kind in PLAUSIBLE
        low, high = PLAUSIBLE[kind]
        assert low < high


def test_reading_your_body_is_separate_from_reading_a_textbook() -> None:
    """A reference article is published; a blood-pressure series is not. Granting the
    first must never imply the second."""
    assert Capability.MEDICAL_READ is not Capability.HEALTH_READ
    assert Capability.HEALTH_READ.touches_private_data
    assert Capability.HEALTH_WRITE.is_effectful


def test_writing_a_reading_is_graded_above_sending_an_email() -> None:
    """It looks disproportionate until you consider what a wrong number does: it is not
    read once and discarded, it joins a series, moves an average, and is still there
    months later when someone reads a trend from it. An email can be apologised for."""
    assert Capability.HEALTH_WRITE.risk.value == "critical"
