"""Heart Rate Measurement `0x2A37` -> `VitalReading`. GSS §3.125.

```
octet 0      Flags     bit 0  Value format: 0 = uint8, 1 = uint16
                       bit 1  Sensor Contact detected
                       bit 2  Sensor Contact supported
                       bit 3  Energy Expended present
                       bit 4  RR-Interval present
1 or 1..2    Heart Rate   uint8 or uint16, bpm
[..2]        Energy Expended   uint16
[..2n]       RR-Intervals      uint16[n], unit 1/1024 s, oldest first
```

Two decode-correctness notes from `research/medical-devices.md` §1.7, both load-bearing:

  * **Bit 1 is meaningless unless bit 2 is set.** "Sensor contact not detected" and
    "this sensor has no contact detection at all" are both encoded as bit 1 = 0; this
    module only emits `VitalStatus.SENSOR_CONTACT_LOST` when bit 2 says contact
    detection exists *and* bit 1 says it is currently absent.
  * **The heart-rate value is a plain `uint8`/`uint16`, never an SFLOAT.** Misreading
    bit 0 (the value-format bit) does not just scale the number wrong — it shifts every
    byte after it by one octet, so Energy Expended and every RR-interval decode to
    garbage that still looks like a well-formed field. This module reads bit 0 exactly
    once, before touching anything else, and there is nowhere else in the function that
    could disagree with it.

Energy Expended and RR-Intervals are consumed (to stay aligned and to validate packet
length) and then discarded — nothing in `ars_protocol.health.VitalKind` represents
either, and GSS 2026 and HRS 1.0 disagree on Energy Expended's unit (joule vs.
kilojoule), so even a future `VitalKind` for it should not trust this field without
resolving that first.
"""

from __future__ import annotations

import struct

from ars_protocol import ReadingSource, VitalKind, VitalReading

from .errors import PacketDecodeError
from .status import VitalStatus

_MIN_LENGTH = 2  # flags + uint8 heart rate


def decode_heart_rate(data: bytes, source: ReadingSource) -> VitalReading:
    """Decodes one `0x2A37` notification into one `VitalReading` (`HEART_RATE`).

    Raises `PacketDecodeError` on a short or over-long packet, for the same reason as
    `blood_pressure.decode_blood_pressure`: a partially-decoded value is worse than none.
    """
    if len(data) < _MIN_LENGTH:
        raise PacketDecodeError(
            f"heart rate measurement too short: {len(data)} octets, need >= {_MIN_LENGTH}"
        )

    flags, offset = data[0], 1
    uint16_format = bool(flags & 0x01)
    contact_supported = bool(flags & 0x04)
    contact_detected = bool(flags & 0x02)
    energy_present = bool(flags & 0x08)
    rr_present = bool(flags & 0x10)

    if uint16_format:
        (bpm,) = struct.unpack_from("<H", data, offset)
        offset += 2
    else:
        bpm = data[offset]
        offset += 1

    if energy_present:
        offset += 2  # Energy Expended, uint16 — discarded, see module docstring

    if rr_present:
        remaining = len(data) - offset
        if remaining <= 0 or remaining % 2 != 0:
            raise PacketDecodeError(
                f"heart rate measurement: {remaining} trailing octets is not a whole "
                "number of RR-intervals"
            )
        offset = len(data)  # RR-intervals (1/1024 s each) consumed; values discarded

    if offset != len(data):
        raise PacketDecodeError(
            f"trailing octets in heart rate measurement: consumed {offset} of {len(data)}"
        )

    status: tuple[str, ...] = ()
    if contact_supported and not contact_detected:
        status = (VitalStatus.SENSOR_CONTACT_LOST.value,)

    return VitalReading(
        kind=VitalKind.HEART_RATE,
        value=float(bpm),
        source=source,
        status=status,
    )


__all__ = ["decode_heart_rate"]
