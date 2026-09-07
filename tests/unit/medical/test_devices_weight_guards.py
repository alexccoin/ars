"""`decode_weight` — `0x2A9D`, WSS 1.0.1. This file is the regression test for two of
the three named bugs in the task brief and in research/medical-devices.md §1.8, both of
which produce numbers `PLAUSIBLE[VitalKind.WEIGHT]` (2.0-400.0 kg) accepts and would
therefore be stored silently as fact if this decoder did not refuse them explicitly.
`decode_weight` is not wired to any live `DeviceBackend` — see its module docstring —
but the guard has to exist and be correct regardless of what calls it.
"""

from __future__ import annotations

import struct

import pytest
from ars_medical.devices.errors import MeasurementUnsuccessful, PacketDecodeError
from ars_medical.devices.weight import decode_weight
from ars_protocol import PLAUSIBLE, VitalKind


def test_si_weight_decodes_correctly():
    reading = decode_weight(bytes([0x00]) + struct.pack("<H", 16000))
    assert reading.kg == 80.0


def test_bug_named_in_the_brief__the_weight_sentinel_0xffff_si():
    """WSS §3.2.1.2: raw 0xFFFF means "Measurement Unsuccessful", not a weight. Naively
    scaled as SI (x0.005) it decodes to 327.675 kg — inside PLAUSIBLE's 2.0-400.0
    range, so the range check alone would store this silently as a real measurement.
    The decoder must refuse it before any scaling happens."""
    low, high = PLAUSIBLE[VitalKind.WEIGHT]
    naive_si_kg = 0xFFFF * 0.005
    assert low <= naive_si_kg <= high, "the whole point: the naive result IS plausible"
    assert round(naive_si_kg, 3) == 327.675  # matches sfloat_check.py's own w5 oracle

    with pytest.raises(MeasurementUnsuccessful):
        decode_weight(bytes([0x00]) + struct.pack("<H", 0xFFFF))


def test_bug_named_in_the_brief__the_weight_sentinel_0xffff_imperial():
    """Same sentinel, Imperial branch: naively scaled it decodes to 297.262 kg — also
    inside PLAUSIBLE, also silently wrong, also refused."""
    low, high = PLAUSIBLE[VitalKind.WEIGHT]
    naive_imperial_kg = 0xFFFF * 0.01 * 0.45359237
    assert low <= naive_imperial_kg <= high
    assert round(naive_imperial_kg, 3) == 297.262

    with pytest.raises(MeasurementUnsuccessful):
        decode_weight(bytes([0x01]) + struct.pack("<H", 0xFFFF))


def test_bug_named_in_the_brief__pounds_recorded_as_kilograms():
    """176.37 lb (raw 17637, Imperial) with the lb -> kg conversion silently skipped
    stores as 176.37 "kg" — inside PLAUSIBLE (2.0-400.0), and a completely different,
    also-plausible number from the true 80.0 kg. `decode_weight` must apply the
    lb -> kg factor unconditionally whenever the units bit says Imperial; there is no
    code path that stops after the wire-scale multiply."""
    low, high = PLAUSIBLE[VitalKind.WEIGHT]
    unconverted_lb_as_kg = 17637 * 0.01
    assert round(unconverted_lb_as_kg, 2) == 176.37
    assert low <= unconverted_lb_as_kg <= high, "the whole point: also plausible"

    reading = decode_weight(bytes([0x01]) + struct.pack("<H", 17637))

    assert round(reading.kg, 3) == 80.0
    assert reading.kg != pytest.approx(unconverted_lb_as_kg), (
        "decode_weight must never return the raw pounds figure relabelled as kg"
    )


def test_full_imperial_packet_with_bmi_and_height():
    """Height must also respect the units bit: inches -> metres, not left in inches."""
    packet = (
        bytes([0x0F])  # imperial, timestamp, user id, bmi+height
        + struct.pack("<H", 16000)
        + struct.pack("<HBBBBB", 2026, 9, 7, 7, 0, 0)
        + bytes([0x01])
        + struct.pack("<H", 247)  # BMI x0.1
        + struct.pack("<H", 705)  # height x0.1 in
    )
    reading = decode_weight(packet)
    assert round(reading.bmi, 1) == 24.7
    assert reading.height_m is not None


def test_si_height_and_bmi_are_not_misread_as_imperial():
    packet = (
        bytes([0x0A])  # SI, timestamp, no user id, bmi+height
        + struct.pack("<H", 16000)
        + struct.pack("<HBBBBB", 2026, 9, 7, 7, 0, 0)
        + struct.pack("<H", 247)
        + struct.pack("<H", 1800)  # height x0.001 m
    )
    reading = decode_weight(packet)
    assert reading.height_m == 1.8
    assert round(reading.bmi, 1) == 24.7


def test_a_short_packet_raises():
    with pytest.raises(PacketDecodeError):
        decode_weight(bytes([0x00, 0x00]))
