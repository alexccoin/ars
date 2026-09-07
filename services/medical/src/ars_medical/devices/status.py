"""Structured device-status identifiers for `VitalReading.status`.

`VitalReading.status` (`packages/protocol/src/ars_protocol/health.py`) is documented as
"stable identifiers... never prose, never translated at this layer; the interface turns
these into sentences in whichever language it is speaking." A decoder in this package
must therefore never write English prose into `VitalReading.note` — that field is the
user's own words, and doing so would (a) be a user-facing string that exists in only one
language, which CLAUDE.md rule 5 forbids, and (b) be indistinguishable from something the
user actually typed. Every decoder in this package writes into `status`, using only the
identifiers below, and leaves `note` untouched (`None`).

`VitalReading.status` is `tuple[str, ...]`, not `tuple[VitalStatus, ...]` — `StrEnum`
members compare and hash equal to their string value, so storing `VitalStatus.X.value`
(a plain `str`) round-trips through the protocol type without needing it to know this
enum exists. `packages/protocol` stays the single source of truth for the *shape*
(`tuple[str, ...]`); this enum is the closed vocabulary this package promises to fill
that shape with, kept next to the decoders that are the only writers of it.
"""

from __future__ import annotations

from enum import StrEnum


class VitalStatus(StrEnum):
    """Every value a decoder in this package may place into `VitalReading.status`.

    Deliberately closed, same reasoning as `VitalKind`: an open string here would mean
    "whatever the decoder happened to write", and a reader trying to render these in EN
    and RO needs a fixed set of keys to have translations for, not a moving target.
    """

    # Blood Pressure Measurement Status (GSS §3.34.3, bits 0/1/2/5)
    BODY_MOVEMENT = "body_movement"
    CUFF_TOO_LOOSE = "cuff_too_loose"
    IRREGULAR_PULSE = "irregular_pulse"
    IMPROPER_MEASUREMENT_POSITION = "improper_measurement_position"
    PULSE_RATE_ABOVE_RANGE = "pulse_rate_above_range"
    PULSE_RATE_BELOW_RANGE = "pulse_rate_below_range"

    # Heart Rate Measurement (GSS §3.125)
    SENSOR_CONTACT_LOST = "sensor_contact_lost"
    """Bit 2 (contact supported) is set and bit 1 (contact detected) is clear — a strap
    that *can* tell you it slipped, telling you it slipped. Meaningless and therefore
    never emitted when the sensor does not support contact detection at all (GSS
    §3.125: bit 1 is not defined unless bit 2 is set)."""

    # Common to any profile with a Date Time field (GSS §3.79)
    DEVICE_CLOCK_UNSET = "device_clock_unset"
    """The device sent year/month/day = 0. The reading is timestamped on arrival
    instead, and this status says so — a reader must not present an arrival timestamp
    as if the device itself had reported that exact moment."""


__all__ = ["VitalStatus"]
