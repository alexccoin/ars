"""`decode_blood_pressure` — `0x2A35`, GSS §3.34. Byte layouts are hand-constructed from
the spec, matching `research/experiments/ble/sfloat_check.py`'s own `decode_bp` test
vectors, so a regression here is also a regression against that independent oracle.
"""

from __future__ import annotations

import struct

import pytest
from ars_medical.devices.blood_pressure import decode_blood_pressure
from ars_medical.devices.errors import PacketDecodeError
from ars_medical.devices.status import VitalStatus
from ars_protocol import ReadingSource, VitalKind, now_ms


def _sf(mantissa: int, exponent: int) -> bytes:
    return struct.pack("<H", ((exponent & 0xF) << 12) | (mantissa & 0x0FFF))


def _datetime(y, mo, d, h, mi, s) -> bytes:
    return struct.pack("<HBBBBB", y, mo, d, h, mi, s)


def _source() -> ReadingSource:
    return ReadingSource()


def test_fully_populated_packet_decodes_systolic_diastolic_and_pulse():
    """Timestamp + pulse + user id + status, mmHg — the 19-octet maximal packet from
    research/medical-devices.md §1.4."""
    packet = (
        bytes([0x1E])
        + _sf(120, 0)
        + _sf(80, 0)
        + _sf(93, 0)  # MAP — decoded to stay aligned, never stored, see module docstring
        + _datetime(2026, 9, 7, 8, 15, 0)
        + _sf(62, 0)
        + bytes([0x01])  # user id — ignored
        + struct.pack("<H", 0x0004)  # irregular pulse bit
    )
    assert len(packet) == 19

    readings = decode_blood_pressure(packet, _source())

    by_kind = {r.kind: r for r in readings}
    assert by_kind[VitalKind.BLOOD_PRESSURE_SYSTOLIC].value == 120.0
    assert by_kind[VitalKind.BLOOD_PRESSURE_DIASTOLIC].value == 80.0
    assert by_kind[VitalKind.HEART_RATE].value == 62.0
    assert VitalKind.BLOOD_PRESSURE_SYSTOLIC in by_kind and len(by_kind) == 3, (
        "Mean Arterial Pressure must never appear as a fourth VitalReading — no "
        "VitalKind represents it (research/medical-devices.md §5.3)"
    )


def test_irregular_pulse_bit_becomes_a_structured_status_never_prose():
    """CLAUDE.md rule 5 + the task brief: device status must be a stable identifier in
    `VitalReading.status`, never English text in `VitalReading.note` — writing prose
    there would be a user-facing string that exists in only one language, and it would
    be indistinguishable from the user's own words."""
    packet = (
        bytes([0x10])  # only "measurement status present"
        + _sf(120, 0)
        + _sf(80, 0)
        + _sf(93, 0)
        + struct.pack("<H", 0x04)  # irregular pulse
    )

    reading = decode_blood_pressure(packet, _source())[0]

    assert reading.status == (VitalStatus.IRREGULAR_PULSE.value,)
    assert reading.note is None


def test_every_measurement_status_bit_maps_to_its_own_identifier():
    packet = (
        bytes([0x10])
        + _sf(120, 0)
        + _sf(80, 0)
        + _sf(93, 0)
        + struct.pack("<H", 0x01 | 0x02 | 0x04 | 0x08 | 0x20)  # movement, loose, irregular,
        # pulse-above-range (bits 3-4 = 01), improper position
    )

    reading = decode_blood_pressure(packet, _source())[0]

    assert set(reading.status) == {
        VitalStatus.BODY_MOVEMENT.value,
        VitalStatus.CUFF_TOO_LOOSE.value,
        VitalStatus.IRREGULAR_PULSE.value,
        VitalStatus.PULSE_RATE_ABOVE_RANGE.value,
        VitalStatus.IMPROPER_MEASUREMENT_POSITION.value,
    }


def test_pulse_below_range_is_distinguished_from_above_range():
    packet = (
        bytes([0x10]) + _sf(120, 0) + _sf(80, 0) + _sf(93, 0) + struct.pack("<H", 0x10)  # 0b10
    )
    reading = decode_blood_pressure(packet, _source())[0]
    assert reading.status == (VitalStatus.PULSE_RATE_BELOW_RANGE.value,)


def test_minimal_seven_octet_packet_has_no_pulse_reading():
    """Flags 0x00: three SFLOATs only, no optional fields. Must decode to exactly
    systolic + diastolic, and must not synthesise a pulse reading from nothing."""
    packet = bytes([0x00]) + _sf(118, 0) + _sf(77, 0) + _sf(90, 0)
    assert len(packet) == 7

    readings = decode_blood_pressure(packet, _source())

    assert {r.kind for r in readings} == {
        VitalKind.BLOOD_PRESSURE_SYSTOLIC,
        VitalKind.BLOOD_PRESSURE_DIASTOLIC,
    }
    assert readings[0].value == 118.0
    assert readings[1].value == 77.0


def test_kpa_device_is_converted_to_mmhg_unconditionally():
    """The units bit (bit 0) is read and applied every time, never assumed — this is
    the exact shape of the "kPa stored as mmHg" trap in research/medical-devices.md
    §1.8: 16.0 kPa stored *as* 16.0 "mmHg" is a device error that PLAUSIBLE happens to
    catch (16 is below the 50 mmHg floor); this test proves the decoder gets the
    conversion right in the first place rather than relying on that catch."""
    packet = bytes([0x01]) + _sf(160, -1) + _sf(107, -1) + _sf(124, -1)  # 16.0 / 10.7 / 12.4 kPa

    readings = decode_blood_pressure(packet, _source())

    systolic = next(r for r in readings if r.kind is VitalKind.BLOOD_PRESSURE_SYSTOLIC)
    assert round(systolic.value, 2) == 120.01  # matches sfloat_check.py's own oracle value


def test_device_clock_unset_is_flagged_and_timestamped_on_arrival():
    """Year/month/day = 0 (GSS §3.79) means the device's clock is not set. The reading
    must not silently adopt a bogus 1970-ish timestamp, and the fact that arrival time
    was substituted must be visible to a reader as a structured status, not lost."""
    before = now_ms()
    packet = (
        bytes([0x02])  # timestamp present, nothing else
        + _sf(131, 0)
        + _sf(84, 0)
        + _sf(99, 0)
        + _datetime(0, 0, 0, 0, 0, 0)
    )

    reading = decode_blood_pressure(packet, _source())[0]
    after = now_ms()

    assert VitalStatus.DEVICE_CLOCK_UNSET.value in reading.status
    assert before <= reading.measured_at_ms <= after


def test_a_short_packet_raises_rather_than_returning_a_partial_reading():
    """A partially decoded blood pressure is worse than none — it looks exactly like a
    real one to everything downstream. research/medical-devices.md's own sketch makes
    this same call; this pins it as a contract, not an implementation detail."""
    with pytest.raises(PacketDecodeError):
        decode_blood_pressure(bytes([0x00, 0x78, 0x00]), _source())


def test_trailing_octets_after_every_declared_field_also_raise():
    """A device sending flags that under-declare its own payload (or the wrong flags
    entirely) must not have the extra bytes silently ignored — that is indistinguishable
    from a version of the profile this decoder was never written against."""
    packet = bytes([0x00]) + _sf(118, 0) + _sf(77, 0) + _sf(90, 0) + b"\x00\x00"
    with pytest.raises(PacketDecodeError):
        decode_blood_pressure(packet, _source())


def test_nan_pulse_field_is_not_stored_as_a_reading():
    """PLXS §5.1: NaN in an SFLOAT subfield means "not measured". A decoder that does
    not check for it would store `float('nan')` as a vital — this must not happen."""
    packet = bytes([0x04]) + _sf(120, 0) + _sf(80, 0) + _sf(93, 0) + struct.pack("<H", 0x07FF)

    readings = decode_blood_pressure(packet, _source())

    assert {r.kind for r in readings} == {
        VitalKind.BLOOD_PRESSURE_SYSTOLIC,
        VitalKind.BLOOD_PRESSURE_DIASTOLIC,
    }
