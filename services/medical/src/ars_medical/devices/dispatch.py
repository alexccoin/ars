"""Shared decode-and-filter path used by both `MockDeviceBackend` and `BleDeviceBackend`.

Kept in one place so the two backends cannot drift on the one behaviour that matters
most: **implausible values are dropped here, before a reading ever reaches a caller**,
using the exact same `VitalReading.is_plausible` (backed by `ars_protocol.health.
PLAUSIBLE`) that `SqliteMedicalStore.record` would apply anyway — dropping it one layer
earlier means a cuff that slipped mid-session never even shows up as "pending
confirmation" in a UI, and the telemetry below is where "was PLAUSIBLE doing anything"
becomes an answerable question instead of a guess.
"""

from __future__ import annotations

from ars_protocol import DeviceKind, ReadingSource, VitalReading
from ars_telemetry import counter

from .blood_pressure import decode_blood_pressure
from .errors import PacketDecodeError
from .heart_rate import decode_heart_rate

_DECODABLE_KINDS = frozenset({DeviceKind.BLOOD_PRESSURE_MONITOR, DeviceKind.HEART_RATE_MONITOR})


def decode_indication(
    device_kind: DeviceKind, data: bytes, source: ReadingSource
) -> list[VitalReading]:
    """Decodes one characteristic payload for `device_kind` into zero or more accepted
    `VitalReading`s. Never raises `PacketDecodeError`/`MeasurementUnsuccessful` outward —
    both are counted and swallowed here, because one malformed or explicitly-failed
    indication in a session must not end the session (research/medical-devices.md
    §3.5's disconnect-is-normal principle, applied to single packets, not just links).
    """
    if device_kind not in _DECODABLE_KINDS:
        raise ValueError(f"no decoder registered for device_kind={device_kind.value!r}")

    try:
        if device_kind is DeviceKind.BLOOD_PRESSURE_MONITOR:
            decoded = decode_blood_pressure(data, source)
        else:
            decoded = [decode_heart_rate(data, source)]
    except PacketDecodeError:
        counter("ars_medical.devices.decode_error").add(1, device_kind=device_kind.value)
        return []

    accepted: list[VitalReading] = []
    for reading in decoded:
        if reading.is_plausible:
            accepted.append(reading)
            counter("ars_medical.devices.reading_decoded").add(1, kind=reading.kind.value)
        else:
            counter("ars_medical.devices.reading_dropped_implausible").add(
                1, kind=reading.kind.value
            )
    return accepted


__all__ = ["decode_indication"]
