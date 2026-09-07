"""`MockDeviceBackend` against `DeviceBackend` (`ars_core.interfaces`) — the seam
required by the task brief, exercised end-to-end without ever touching a radio, per
"do not connect to real hardware... everything is tested against the mock backend".
"""

from __future__ import annotations

import struct

from ars_core import DeviceBackend
from ars_medical.devices import MockDeviceBackend
from ars_protocol import DeviceKind, VitalKind


def _sf(mantissa: int, exponent: int) -> bytes:
    return struct.pack("<H", ((exponent & 0xF) << 12) | (mantissa & 0x0FFF))


_NORMAL_BP = (
    bytes([0x1E])
    + _sf(120, 0)
    + _sf(80, 0)
    + _sf(93, 0)
    + struct.pack("<HBBBBB", 2026, 9, 7, 8, 15, 0)
    + _sf(62, 0)
    + bytes([0xFF])
    + struct.pack("<H", 0x0004)
)
_CUFF_SLIPPED = bytes([0x00]) + _sf(20, 0) + _sf(10, 0) + _sf(13, 0)  # implausible: 20/10
_MINIMAL_BP = bytes([0x00]) + _sf(118, 0) + _sf(77, 0) + _sf(90, 0)


def test_mock_device_backend_is_a_real_device_backend():
    assert isinstance(MockDeviceBackend(DeviceKind.BLOOD_PRESSURE_MONITOR, []), DeviceBackend)


async def test_discover_yields_a_candidate_of_the_configured_kind():
    backend = MockDeviceBackend(DeviceKind.BLOOD_PRESSURE_MONITOR, [], name="A&D UA-651BLE")
    candidates = [c async for c in backend.discover()]
    assert len(candidates) == 1
    assert candidates[0].device_kind is DeviceKind.BLOOD_PRESSURE_MONITOR
    assert candidates[0].name == "A&D UA-651BLE"


async def test_read_decodes_and_tags_every_reading_with_the_candidate_as_source():
    backend = MockDeviceBackend(
        DeviceKind.BLOOD_PRESSURE_MONITOR, [_NORMAL_BP], transport_id="MOCK-BP-1"
    )
    candidate = await anext(backend.discover())

    readings = [r async for r in backend.read(candidate)]

    assert {r.kind for r in readings} == {
        VitalKind.BLOOD_PRESSURE_SYSTOLIC,
        VitalKind.BLOOD_PRESSURE_DIASTOLIC,
        VitalKind.HEART_RATE,
    }
    assert all(r.source.device_id == "MOCK-BP-1" for r in readings)
    assert all(r.source.device_kind is DeviceKind.BLOOD_PRESSURE_MONITOR for r in readings)


async def test_implausible_readings_are_dropped_before_they_reach_the_caller():
    """A cuff-slipped 20/10 packet must never surface as a `VitalReading` at all —
    dropped one layer before the store even sees it, same `PLAUSIBLE` gate
    `SqliteMedicalStore.record` applies, applied earlier."""
    backend = MockDeviceBackend(
        DeviceKind.BLOOD_PRESSURE_MONITOR, [_NORMAL_BP, _CUFF_SLIPPED, _MINIMAL_BP]
    )
    candidate = await anext(backend.discover())

    readings = [r async for r in backend.read(candidate)]

    # 3 (normal: sys/dia/pulse) + 0 (cuff slipped, both halves implausible) + 2 (minimal)
    assert len(readings) == 5
    assert all(r.is_plausible for r in readings)


async def test_a_malformed_packet_is_skipped_not_fatal_to_the_session():
    """One bad indication in a session must not end it — the decode error is counted
    (see `dispatch.decode_indication`) and the loop continues to the next packet."""
    too_short = bytes([0x00, 0x01])
    backend = MockDeviceBackend(
        DeviceKind.BLOOD_PRESSURE_MONITOR, [too_short, _MINIMAL_BP]
    )
    candidate = await anext(backend.discover())

    readings = [r async for r in backend.read(candidate)]

    assert len(readings) == 2  # only the minimal packet's systolic + diastolic


async def test_disconnect_after_a_reading_ends_the_session_without_raising():
    """Several real devices deliberately drop the link right after delivering a
    reading (research/medical-devices.md §3.5) — that must read as a normal end of
    iteration, not an exception. `disconnect_after=1` simulates exactly that."""
    backend = MockDeviceBackend(
        DeviceKind.BLOOD_PRESSURE_MONITOR, [_NORMAL_BP, _MINIMAL_BP], disconnect_after=1
    )
    candidate = await anext(backend.discover())

    readings = [r async for r in backend.read(candidate)]  # must not raise

    assert len(readings) == 3  # only _NORMAL_BP's three readings; _MINIMAL_BP never sent


async def test_heart_rate_monitor_kind_decodes_via_the_heart_rate_path():
    backend = MockDeviceBackend(DeviceKind.HEART_RATE_MONITOR, [bytes([0x00, 68])])
    candidate = await anext(backend.discover())

    readings = [r async for r in backend.read(candidate)]

    assert len(readings) == 1
    assert readings[0].kind is VitalKind.HEART_RATE
    assert readings[0].value == 68.0


def test_supported_kinds_never_overstates_what_this_backend_decodes():
    bp = MockDeviceBackend(DeviceKind.BLOOD_PRESSURE_MONITOR, [])
    hr = MockDeviceBackend(DeviceKind.HEART_RATE_MONITOR, [])
    assert VitalKind.HEART_RATE in bp.supported_kinds  # free from the pulse field
    assert VitalKind.BLOOD_PRESSURE_SYSTOLIC not in hr.supported_kinds
