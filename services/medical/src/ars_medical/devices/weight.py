"""Weight Measurement `0x2A9D` -> `VitalReading`. GSS §3.275, WSS 1.0.1 §3.2.1.2.

**No `DeviceBackend` in this package connects to `0x181D` yet** — `research/medical-
devices.md` §2.2/§5.5 recommends against building a live Weight Scale backend for
increment 1: WSS §3.2.1 defines the service as single-client ("once transfer of a
measurement is successful, the measurement shall not be retransmitted"), so if Alex's
phone app is bonded first, A.R.S never sees a reading, non-deterministically, day to
day — and almost nothing on the consumer market implements the standard service anyway.

This module exists regardless, decode-only, because the task brief is explicit that the
decoder guards below — not the BLE plumbing — are the point: two of the three
non-negotiable unit/sentinel bugs named in `research/medical-devices.md` §1.8 live
here, they produce numbers `PLAUSIBLE` accepts, and a `VitalReading` may reach this
store from `DeviceKind.MANUAL` entry or a future scale backend without ever touching a
radio. The guards must hold regardless of what calls this function.

```
octet 0      Flags     bit 0  Units.  0 = SI (kg, m), 1 = Imperial (lb, in)
                       bit 1  Time Stamp present
                       bit 2  User ID present          <- BP uses bit 2 for Pulse Rate
                       bit 3  BMI AND Height present   <- BP uses bit 3 for User ID
1..2         Weight    uint16.  x 0.005 kg  (SI)  or  x 0.01 lb  (Imperial)
[3..9]       Time Stamp   Date Time, 7 octets   if bit 1
[..+1]       User ID      uint8                 if bit 2
[..+2]       BMI          uint16 x 0.1 kg/m^2    if bit 3
[..+2]       Height       uint16 x 0.001 m (SI) or x 0.1 in (Imperial)   if bit 3
```

The flag bit order is **not** the same as Blood Pressure's — bit 2 and bit 3 swap
meaning between the two characteristics. A decoder that shares bit-mask constants
between `blood_pressure.py` and this module would silently read the wrong optional
fields; the two modules intentionally do not share flag-bit code.

**Bug 1 — the weight sentinel (`research/medical-devices.md` §1.8).** `raw == 0xFFFF`
means WSS's "Measurement Unsuccessful" (WSS §3.2.1.2), not a weight of 327.675 kg (SI)
or 297.262 kg (Imperial). `PLAUSIBLE[VitalKind.WEIGHT]` is `(2.0, 400.0)` — both decoded
sentinel values sit inside it and would be stored silently as fact. This module checks
`raw == 0xFFFF` *before* any scaling and raises `MeasurementUnsuccessful`; there is no
code path in this module that can produce 327.675 or 297.262.

**Bug 2 — pounds stored as kilograms.** `176.37` for an 80 kg person is not a rounding
error, it is the raw Imperial reading (`17637 x 0.01 = 176.37 lb`) with the mandatory
lb -> kg conversion (`x 0.45359237`) skipped — as if the units bit were read to select
*which scale factor to apply to the wire integer* but not *which unit that produces*.
`176.37` sits inside `PLAUSIBLE[VitalKind.WEIGHT]` (2.0-400.0) and would be stored
silently as fact. `VitalKind.WEIGHT`'s unit is always kg (`ars_protocol.health`); this
module's return type only ever carries a kg value — there is no accessor that returns
an unconverted pound figure, so a caller cannot skip the conversion even by mistake.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from . import datetime_codec, gatt
from .errors import MeasurementUnsuccessful, PacketDecodeError

_MIN_LENGTH = 3  # flags + uint16 weight
_UNSUCCESSFUL_RAW = 0xFFFF


@dataclass(frozen=True, slots=True)
class WeightMeasurement:
    """A successfully decoded weight, always in kg regardless of the wire units bit."""

    kg: float
    measured_at_ms: int | None
    clock_unset: bool
    bmi: float | None
    height_m: float | None


def decode_weight(data: bytes) -> WeightMeasurement:
    """Decodes one `0x2A9D` indication.

    Raises `MeasurementUnsuccessful` for the `0xFFFF` sentinel, checked before any
    scaling — see Bug 1 above. Raises `PacketDecodeError` on a short or over-long
    packet. Never returns an unconverted pounds figure — see Bug 2 above.
    """
    if len(data) < _MIN_LENGTH:
        raise PacketDecodeError(
            f"weight measurement too short: {len(data)} octets, need >= {_MIN_LENGTH}"
        )

    flags, offset = data[0], 1
    imperial = bool(flags & 0x01)

    (raw_weight,) = struct.unpack_from("<H", data, offset)
    offset += 2

    if raw_weight == _UNSUCCESSFUL_RAW:
        raise MeasurementUnsuccessful(
            "weight measurement unsuccessful (0xFFFF sentinel, WSS section 3.2.1.2) — "
            "not a reading, refused before any unit scaling"
        )

    # Convert on the units bit unconditionally: compute the wire value in its native
    # unit, then always finish with the lb -> kg factor when that native unit is
    # pounds. There is no branch that stops after the wire-scale multiply and calls
    # the result kg — that half-conversion is exactly Bug 2.
    kg = raw_weight * 0.01 * gatt.LB_TO_KG if imperial else raw_weight * 0.005

    measured_at_ms: int | None = None
    clock_unset = False
    if flags & 0x02:
        measured_at_ms, offset = datetime_codec.decode(data, offset)
        clock_unset = measured_at_ms is None

    if flags & 0x04:
        offset += 1  # User ID — ignored, single-user store

    bmi: float | None = None
    height_m: float | None = None
    if flags & 0x08:
        (raw_bmi,) = struct.unpack_from("<H", data, offset)
        offset += 2
        bmi = raw_bmi * 0.1
        (raw_height,) = struct.unpack_from("<H", data, offset)
        offset += 2
        height_m = raw_height * 0.1 * 0.0254 if imperial else raw_height * 0.001

    if offset != len(data):
        raise PacketDecodeError(
            f"trailing octets in weight measurement: consumed {offset} of {len(data)}"
        )

    return WeightMeasurement(
        kg=kg, measured_at_ms=measured_at_ms, clock_unset=clock_unset, bmi=bmi, height_m=height_m
    )


__all__ = ["WeightMeasurement", "decode_weight"]
