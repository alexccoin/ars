"""`decode_heart_rate` — `0x2A37`, GSS §3.125. Byte layouts match
`research/experiments/ble/sfloat_check.py`'s own `decode_hr` test vectors.
"""

from __future__ import annotations

import struct

import pytest
from ars_medical.devices.errors import PacketDecodeError
from ars_medical.devices.heart_rate import decode_heart_rate
from ars_medical.devices.status import VitalStatus
from ars_protocol import ReadingSource, VitalKind


def _source() -> ReadingSource:
    return ReadingSource()


def test_uint8_value_format():
    reading = decode_heart_rate(bytes([0x00, 62]), _source())
    assert reading.kind is VitalKind.HEART_RATE
    assert reading.value == 62.0


def test_uint16_value_format():
    """A misread of bit 0 does not just scale the number — it shifts every following
    octet, so a `uint16` reading decoded as `uint8` produces a plausible-looking wrong
    value from the low byte alone. This packet's value (300) is out of `PLAUSIBLE`
    range and would be caught downstream regardless, but the decode itself must still
    be exactly right: 300, not the low byte of it."""
    reading = decode_heart_rate(bytes([0x01]) + struct.pack("<H", 300), _source())
    assert reading.value == 300.0


def test_rr_intervals_are_consumed_but_do_not_shift_the_heart_rate_value():
    packet = bytes([0x16, 62]) + struct.pack("<HH", 1024, 990)  # uint16 flag off, RR present
    reading = decode_heart_rate(packet, _source())
    assert reading.value == 62.0


def test_contact_supported_and_detected_reports_no_status():
    packet = bytes([0x04 | 0x02, 62])  # contact supported AND detected
    reading = decode_heart_rate(packet, _source())
    assert reading.status == ()


def test_contact_supported_but_not_detected_is_sensor_contact_lost():
    packet = bytes([0x04, 62])  # contact supported, bit 1 clear -> not detected
    reading = decode_heart_rate(packet, _source())
    assert reading.status == (VitalStatus.SENSOR_CONTACT_LOST.value,)


def test_contact_not_supported_never_reports_status_even_with_bit1_clear():
    """Bit 1 is meaningless unless bit 2 is set (research/medical-devices.md §1.7): a
    strap with no contact-detection hardware at all also reports bit 1 = 0, and that
    must not be confused with "contact lost" — there is no sensor to have lost it."""
    packet = bytes([0x00, 62])  # neither bit set
    reading = decode_heart_rate(packet, _source())
    assert reading.status == ()


def test_energy_expended_is_consumed_without_shifting_rr_intervals():
    packet = bytes([0x18, 62]) + struct.pack("<H", 500) + struct.pack("<H", 1024)
    reading = decode_heart_rate(packet, _source())
    assert reading.value == 62.0


def test_a_short_packet_raises():
    with pytest.raises(PacketDecodeError):
        decode_heart_rate(bytes([0x00]), _source())


def test_an_odd_number_of_trailing_rr_octets_raises_rather_than_truncating():
    """RR-intervals are uint16 each; an odd remainder after the declared fields means
    either a misread flag or a corrupted packet — silently dropping the last byte would
    hide that instead of surfacing it."""
    packet = bytes([0x10, 62]) + b"\x00\x04\x00"  # 3 trailing octets, not a multiple of 2
    with pytest.raises(PacketDecodeError):
        decode_heart_rate(packet, _source())
