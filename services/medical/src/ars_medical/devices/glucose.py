"""Glucose Measurement `0x2A18` -> mmol/L. GSS §3.118, GLS 1.0.1 §3.1.1.5.

**No `DeviceBackend` in this package connects to `0x1808` yet** — `research/medical-
devices.md` §1.2/§5.5 recommends deferring glucose for increment 1: the measurement
characteristic notifies nothing until a Record Access Control Point state machine is
driven (GLS §3.1.1: "subscribing alone yields nothing forever"), which is out of scope
here. This module exists regardless, decode-only, for the same reason `weight.py` does:
the guard is the point, not the radio.

```
octet 0      Flags     bit 0  Time Offset present
                       bit 1  Glucose Concentration AND Type-Sample Location present
                       bit 2  Units.  0 = kg/L base unit (displayed mg/dL)
                                      1 = mol/L base unit (displayed mmol/L)
                       bit 3  Sensor Status Annunciation present
                       bit 4  Context Information follows (a separate 0x2A34 arrives)
1..2         Sequence Number             uint16
3..9         Base Time                   Date Time, 7 octets  (always present)
[10..11]     Time Offset                 sint16, minutes       if bit 0
[..+2]       Glucose Concentration       medfloat16            if bit 1
[..+1]       Type / Sample Location      uint8                 if bit 1
[..+2]       Sensor Status Annunciation  boolean[16]           if bit 3
```

**Bug 3 — the unit bit determines the conversion *pipeline*, not just a display
label, and skipping the pipeline produces a plausible-looking wrong number.** GSS's
2026-02-05 field table and its own §3.118.1 prose *disagree* about what bit 2 means
(`research/medical-devices.md` §1.6); GLS 1.0.1 §3.1.1.5 is the document that actually
settles it and is quoted here because it is the load-bearing sentence for this whole
module:

    "If the unit of the Glucose Concentration is in base units of kg/L (typically
    displayed in units of mg/dL), bit 2 of the Flags field is set to 0. Otherwise, the
    unit is in base units of mol/L (typically displayed in units of mmol/L) and bit 2
    of the Flags field is set to 1. ... when a Glucose Concentration value in units of
    mg/dL is converted to units of kg/L, the SFLOAT exponent will need to be adjusted
    by subtracting 5. Similarly, when a Glucose Concentration value in units of mmol/L
    is converted to units of mol/L, the SFLOAT exponent will need to be adjusted by
    subtracting 3."

So the decoded SFLOAT is in **kg/L or mol/L**, never directly in mg/dL or mmol/L, and:

  * bit 2 = 0: `mg/dL = value * 1e5`, then `mmol/L = mg/dL / 18.0156`
  * bit 2 = 1: `mmol/L = value * 1e3`

A decoder that treats the decoded SFLOAT as if it were *already* the displayed unit
(skipping the `1e5`/`1e3` pipeline above) is silently wrong by three to five orders of
magnitude depending on which branch — and, worse, some of those wrong outputs still
land inside `PLAUSIBLE[VitalKind.BLOOD_GLUCOSE]` (1.0-40.0 mmol/L). The concrete,
byte-level regression for this is in `tests/unit/medical/test_devices_glucose.py`
(`test_naive_mantissa_read_is_ten_times_wrong_and_still_plausible`), reproducing the
exact case `research/medical-devices.md` §1.6 names: a genuine 3.0 mmol/L device
(mantissa 30, exponent -4) whose raw mantissa, read without the exponent and pipeline
above, is 30 — ten times the true value and still inside range.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from . import datetime_codec
from .errors import PacketDecodeError
from .ieee11073 import read_sfloat

_MIN_LENGTH = 9  # flags(1) + sequence(2) + base time(7), concentration always optional
_MG_DL_PER_KG_PER_L = 1e5
_MMOL_PER_MOL = 1e3
_MG_DL_PER_MMOL_L = 18.0156


@dataclass(frozen=True, slots=True)
class GlucoseMeasurement:
    """A successfully decoded glucose reading, always in mmol/L — the unit
    `VitalKind.BLOOD_GLUCOSE` uses (`ars_protocol.health`) — regardless of which base
    unit the device transmitted in."""

    sequence: int
    mmol_l: float | None
    """`None` when the Glucose Concentration field itself was absent (flags bit 1
    clear) — a context-only record, not a missing conversion. Never a NaN float."""
    base_time_ms: int | None
    clock_unset: bool


def decode_glucose(data: bytes) -> GlucoseMeasurement:
    """Decodes one `0x2A18` notification.

    Raises `PacketDecodeError` on a short or over-long packet. Never returns a
    concentration that skipped the unit pipeline described in this module's
    docstring — see Bug 3 above.
    """
    if len(data) < _MIN_LENGTH:
        raise PacketDecodeError(
            f"glucose measurement too short: {len(data)} octets, need >= {_MIN_LENGTH}"
        )

    flags, offset = data[0], 1
    (sequence,) = struct.unpack_from("<H", data, offset)
    offset += 2
    base_time_ms, offset = datetime_codec.decode(data, offset)
    clock_unset = base_time_ms is None

    if flags & 0x01:
        offset += 2  # Time Offset, sint16 minutes — not needed for the value itself

    mmol_l: float | None = None
    if flags & 0x02:
        raw_value, offset = read_sfloat(data, offset)
        mol_l_units = bool(flags & 0x04)
        # The unit bit selects the conversion *pipeline*, applied in full every time —
        # never a shortcut that treats `raw_value` as already being the displayed
        # unit. See "Bug 3" above.
        if mol_l_units:
            mmol_l = raw_value * _MMOL_PER_MOL
        else:
            mg_dl = raw_value * _MG_DL_PER_KG_PER_L
            mmol_l = mg_dl / _MG_DL_PER_MMOL_L
        offset += 1  # Type (low nibble) / Sample Location (high nibble) — not stored

    if flags & 0x08:
        offset += 2  # Sensor Status Annunciation — no VitalStatus mapping defined yet

    if offset != len(data):
        raise PacketDecodeError(
            f"trailing octets in glucose measurement: consumed {offset} of {len(data)}"
        )

    return GlucoseMeasurement(
        sequence=sequence, mmol_l=mmol_l, base_time_ms=base_time_ms, clock_unset=clock_unset
    )


__all__ = ["GlucoseMeasurement", "decode_glucose"]
