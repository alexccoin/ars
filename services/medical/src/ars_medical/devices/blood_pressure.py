"""Blood Pressure Measurement `0x2A35` -> `VitalReading`s. GSS §3.34, Table 3.52.

```
octet 0      Flags                       boolean[8]
             bit 0  Units.  0 = mmHg, 1 = kPa
             bit 1  Time Stamp present
             bit 2  Pulse Rate present
             bit 3  User ID present
             bit 4  Measurement Status present
1..2         Systolic                    medfloat16   (mmHg if bit0=0, else kPa)
3..4         Diastolic                   medfloat16
5..6         Mean Arterial Pressure      medfloat16
[7..13]      Time Stamp                  Date Time, 7 octets   if bit 1
[..+2]       Pulse Rate                  medfloat16            if bit 2
[..+1]       User ID                     uint8                 if bit 3
[..+2]       Measurement Status          boolean[16]           if bit 4
```

Minimum legal packet: 7 octets. Fully populated: 19 octets. Both are asserted in
`research/experiments/ble/sfloat_check.py` and reproduced in
`tests/unit/medical/test_devices_blood_pressure.py`.

One reading in, up to three `VitalReading`s out (systolic, diastolic, and pulse when
present) — `VitalKind` is scalar, so a compound characteristic decodes to several
records sharing one `measured_at_ms`, never one record with three numbers in it. Mean
Arterial Pressure is decoded (it must be, to stay aligned on the wire) and then
discarded: no `VitalKind` exists for it, and nothing in this system would ever read it
— see `research/medical-devices.md` §5.3 for why that omission is deliberate rather than
an oversight.
"""

from __future__ import annotations

import struct

from ars_protocol import ReadingSource, VitalKind, VitalReading, now_ms

from . import datetime_codec, gatt
from .errors import PacketDecodeError
from .ieee11073 import read_sfloat
from .status import VitalStatus

_MIN_LENGTH = 7

# Measurement Status bit positions, GSS §3.34.3.
_STATUS_BODY_MOVEMENT = 0x01
_STATUS_CUFF_TOO_LOOSE = 0x02
_STATUS_IRREGULAR_PULSE = 0x04
_STATUS_PULSE_ABOVE_RANGE = 0x08  # bits 3-4 = 0b01
_STATUS_PULSE_BELOW_RANGE = 0x10  # bits 3-4 = 0b10
_STATUS_IMPROPER_POSITION = 0x20


def _measurement_status_flags(raw: int) -> tuple[str, ...]:
    flags: list[str] = []
    if raw & _STATUS_BODY_MOVEMENT:
        flags.append(VitalStatus.BODY_MOVEMENT.value)
    if raw & _STATUS_CUFF_TOO_LOOSE:
        flags.append(VitalStatus.CUFF_TOO_LOOSE.value)
    if raw & _STATUS_IRREGULAR_PULSE:
        flags.append(VitalStatus.IRREGULAR_PULSE.value)
    pulse_range_bits = raw & 0x18
    if pulse_range_bits == _STATUS_PULSE_ABOVE_RANGE:
        flags.append(VitalStatus.PULSE_RATE_ABOVE_RANGE.value)
    elif pulse_range_bits == _STATUS_PULSE_BELOW_RANGE:
        flags.append(VitalStatus.PULSE_RATE_BELOW_RANGE.value)
    if raw & _STATUS_IMPROPER_POSITION:
        flags.append(VitalStatus.IMPROPER_MEASUREMENT_POSITION.value)
    return tuple(flags)


def decode_blood_pressure(data: bytes, source: ReadingSource) -> list[VitalReading]:
    """Decodes one `0x2A35` indication. Returns systolic, diastolic, and (if present)
    pulse as separate `VitalReading`s, oldest-field-order.

    Raises `PacketDecodeError` on a short or over-long packet rather than returning a
    partial result — a partially decoded blood pressure is worse than none, because it
    looks exactly like a real one to everything downstream.
    """
    if len(data) < _MIN_LENGTH:
        raise PacketDecodeError(
            f"blood pressure measurement too short: {len(data)} octets, need >= {_MIN_LENGTH}"
        )

    flags, offset = data[0], 1
    kpa = bool(flags & 0x01)

    systolic, offset = read_sfloat(data, offset)
    diastolic, offset = read_sfloat(data, offset)
    # decoded to stay aligned with the wire format; not stored — see module docstring
    _map, offset = read_sfloat(data, offset)

    # Convert on the units bit unconditionally — never assume mmHg. This is the exact
    # shape of the kPa-stored-as-mmHg trap (research/medical-devices.md §1.8): skip
    # this branch and every kPa device's numbers are ~7.5x too small and, for many
    # real readings, still fall inside PLAUSIBLE.
    if kpa:
        systolic *= gatt.KPA_TO_MMHG
        diastolic *= gatt.KPA_TO_MMHG

    measured_at_ms: int | None = None
    clock_unset = False
    if flags & 0x02:
        measured_at_ms, offset = datetime_codec.decode(data, offset)
        clock_unset = measured_at_ms is None

    pulse: float | None = None
    if flags & 0x04:
        pulse, offset = read_sfloat(data, offset)

    if flags & 0x08:
        offset += 1  # User ID — ignored: single-user store, nothing here reads it

    status_flags: tuple[str, ...] = ()
    if flags & 0x10:
        (raw_status,) = struct.unpack_from("<H", data, offset)
        offset += 2
        status_flags = _measurement_status_flags(raw_status)

    if offset != len(data):
        raise PacketDecodeError(
            f"trailing octets in blood pressure measurement: consumed {offset} of {len(data)}"
        )

    if clock_unset:
        status_flags = (*status_flags, VitalStatus.DEVICE_CLOCK_UNSET.value)

    at_ms = measured_at_ms if measured_at_ms is not None else now_ms()

    readings = [
        VitalReading(
            kind=VitalKind.BLOOD_PRESSURE_SYSTOLIC,
            value=systolic,
            measured_at_ms=at_ms,
            source=source,
            status=status_flags,
        ),
        VitalReading(
            kind=VitalKind.BLOOD_PRESSURE_DIASTOLIC,
            value=diastolic,
            measured_at_ms=at_ms,
            source=source,
            status=status_flags,
        ),
    ]
    if pulse is not None and pulse == pulse:  # NaN ("not measured", PLXS §5.1) excluded
        readings.append(
            VitalReading(
                kind=VitalKind.HEART_RATE,
                value=pulse,
                measured_at_ms=at_ms,
                source=source,
                status=status_flags,
            )
        )
    return readings


__all__ = ["decode_blood_pressure"]
