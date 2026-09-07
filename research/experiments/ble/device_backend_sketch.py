"""Proposed `DeviceBackend` seam + a BLE implementation sketch + a mock, exercised.

Run: uv run python research/experiments/ble/device_backend_sketch.py

NOTHING HERE OPENS A RADIO. `BleDeviceBackend` is a sketch whose import of bleak is
guarded; only `MockDeviceBackend` is executed, and it replays hand-built packets. This
file exists to prove the seam and the decode produce valid `VitalReading` objects against
the real `ars_protocol` types, not to talk to hardware.
"""

from __future__ import annotations

import asyncio
import struct
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated

from pydantic import Field

from ars_protocol.common import Model, new_id, now_ms
from ars_protocol.health import DeviceKind, ReadingSource, VitalKind, VitalReading

# --------------------------------------------------------------------------------------
# Would live in packages/protocol/src/ars_protocol/health.py
# --------------------------------------------------------------------------------------


class DeviceCandidate(Model):
    """A device seen advertising. Not yet connected, not yet trusted."""

    id: Annotated[str, Field(default_factory=lambda: new_id("dev"))]
    transport_id: str
    """Opaque, backend-scoped. On macOS/CoreBluetooth this is the per-Mac CBPeripheral
    UUID, NOT a MAC address, and it differs on every machine that scans."""
    name: str | None = None
    device_kind: DeviceKind = DeviceKind.MANUAL
    services: tuple[str, ...] = ()
    rssi: int | None = None


# --------------------------------------------------------------------------------------
# Would live in packages/core/src/ars_core/interfaces.py
# --------------------------------------------------------------------------------------


class DeviceBackend(ABC):
    """A source of `VitalReading`s from physical equipment.

    One method yields readings; that is the whole seam. Note what it cannot do: it cannot
    write to a device, cannot take a memory record, and cannot be asked for a diagnosis.
    Per CLAUDE.md rule 2, `bleak` is imported only inside an implementation of this class.
    Per rule 3, callers reach this only after a `GuardEngine.evaluate` ALLOW; the skill
    that wraps it re-checks.
    """

    @property
    @abstractmethod
    def supported_kinds(self) -> frozenset[VitalKind]:
        """Which `VitalKind`s this backend can actually produce. A backend that lists a
        kind it has never decoded from a real device is lying to the router."""

    @abstractmethod
    def discover(self, *, timeout_s: float = 10.0) -> AsyncIterator[DeviceCandidate]:
        """Advertising devices matching this backend's known service UUIDs."""

    @abstractmethod
    def read(
        self, candidate: DeviceCandidate, *, timeout_s: float = 120.0
    ) -> AsyncIterator[VitalReading]:
        """One measurement session. Yields as readings arrive, ends on device disconnect,
        session completion or timeout.

        Declared `def`, not `async def`, for the same reason as `LlmBackend.complete`.

        The generous default timeout is deliberate: a blood-pressure cuff takes ~40 s to
        inflate and settle, and on macOS the first access to an encrypted characteristic
        blocks on a system pairing dialog the user has to physically answer.
        """

    @property
    @abstractmethod
    def is_standard_profile(self) -> bool:
        """True if this backend speaks an adopted Bluetooth SIG profile rather than a
        reverse-engineered vendor protocol. The UI must be able to say which, because a
        reverse-engineered decode can silently change meaning after a firmware update."""


# --------------------------------------------------------------------------------------
# medfloat16 / medfloat32 (IEEE 11073-20601) — see sfloat_check.py for the test vectors
# --------------------------------------------------------------------------------------

_SFLOAT_SPECIAL = {0x07FF: float("nan"), 0x0800: float("nan"), 0x0801: float("nan"),
                   0x07FE: float("inf"), 0x0802: float("-inf")}


def sfloat(raw: int) -> float:
    if raw in _SFLOAT_SPECIAL:
        return _SFLOAT_SPECIAL[raw]
    exp, mant = raw >> 12, raw & 0x0FFF
    if exp >= 0x8:
        exp -= 0x10
    if mant >= 0x800:
        mant -= 0x1000
    return mant * (10.0**exp)


def _datetime_ms(y: int, mo: int, d: int, h: int, mi: int, s: int) -> int | None:
    """Date Time (0x2A08) -> epoch ms, or None if the device's clock is unset.

    Date Time carries NO time zone. It is local wall time on the device. Interpreting it
    in the host's current zone is the only option and is wrong across a DST boundary or a
    trip abroad; a device with an unset clock reports year 0 and must not be trusted at
    all. When this returns None the caller should stamp arrival time and say so.
    """
    if y == 0 or mo == 0 or d == 0:
        return None
    try:
        return int(datetime(y, mo, d, h, mi, s).astimezone().timestamp() * 1000)
    except ValueError:
        return None


# --------------------------------------------------------------------------------------
# Decode: 0x2A35 Blood Pressure Measurement -> VitalReading[]
# --------------------------------------------------------------------------------------

BLOOD_PRESSURE_SERVICE = "00001810-0000-1000-8000-00805f9b34fb"
BLOOD_PRESSURE_MEASUREMENT = "00002a35-0000-1000-8000-00805f9b34fb"
KPA_TO_MMHG = 7.50061682704


def decode_blood_pressure(data: bytes, source: ReadingSource) -> list[VitalReading]:
    """Decode one 0x2A35 indication. Returns systolic, diastolic and (if present) pulse.

    Raises ValueError rather than guessing: a short packet is a bug or a different device,
    and a partially-decoded blood pressure is worse than none.
    """
    if len(data) < 7:
        raise ValueError(f"blood pressure measurement too short: {len(data)} octets")

    flags, i = data[0], 1
    kpa = bool(flags & 0x01)

    def take_sfloat() -> float:
        nonlocal i
        v = sfloat(struct.unpack_from("<H", data, i)[0])
        i += 2
        return v

    systolic, diastolic, _map = take_sfloat(), take_sfloat(), take_sfloat()
    if kpa:
        systolic, diastolic = systolic * KPA_TO_MMHG, diastolic * KPA_TO_MMHG

    measured_at = None
    if flags & 0x02:
        measured_at = _datetime_ms(*struct.unpack_from("<HBBBBB", data, i))
        i += 7

    pulse = None
    if flags & 0x04:
        pulse = take_sfloat()
    if flags & 0x08:
        i += 1                                    # User ID, ignored: one user here
    status = None
    if flags & 0x10:
        status = struct.unpack_from("<H", data, i)[0]
        i += 2
    if i != len(data):
        raise ValueError(f"trailing octets in 0x2A35: consumed {i} of {len(data)}")

    note_parts = []
    if status is not None:
        if status & 0x01:
            note_parts.append("body movement during measurement")
        if status & 0x02:
            note_parts.append("cuff too loose")
        if status & 0x04:
            note_parts.append("irregular pulse detected")
        if status & 0x20:
            note_parts.append("improper measurement position")
    if measured_at is None and flags & 0x02:
        note_parts.append("device clock unset; timestamped on arrival")
    note = "; ".join(note_parts) or None

    at = measured_at if measured_at is not None else now_ms()
    out = [
        VitalReading(kind=VitalKind.BLOOD_PRESSURE_SYSTOLIC, value=systolic,
                     measured_at_ms=at, source=source, note=note),
        VitalReading(kind=VitalKind.BLOOD_PRESSURE_DIASTOLIC, value=diastolic,
                     measured_at_ms=at, source=source, note=note),
    ]
    if pulse is not None and pulse == pulse:      # NaN means "not measured"
        out.append(VitalReading(kind=VitalKind.HEART_RATE, value=pulse,
                                measured_at_ms=at, source=source, note=note))
    return out


# --------------------------------------------------------------------------------------
# BLE implementation — SKETCH. Not executed here. bleak is imported inside the class.
# --------------------------------------------------------------------------------------


class BleDeviceBackend(DeviceBackend):
    """Standard-GATT BLE devices via bleak/CoreBluetooth.

    The only place in A.R.S that may import bleak (CLAUDE.md rule 2).
    """

    def __init__(self) -> None:
        from bleak import BleakClient, BleakScanner          # noqa: PLC0415  (rule 2)

        self._Client, self._Scanner = BleakClient, BleakScanner

    @property
    def supported_kinds(self) -> frozenset[VitalKind]:
        return frozenset({VitalKind.BLOOD_PRESSURE_SYSTOLIC,
                          VitalKind.BLOOD_PRESSURE_DIASTOLIC,
                          VitalKind.HEART_RATE})

    @property
    def is_standard_profile(self) -> bool:
        return True

    async def discover(self, *, timeout_s: float = 10.0) -> AsyncIterator[DeviceCandidate]:
        # service_uuids is not just an optimisation on macOS: 12.0-12.2 return no
        # advertisement data at all without it, and filtering keeps every other BLE
        # device in the flat out of the log.
        found = await self._Scanner.discover(
            timeout=timeout_s, service_uuids=[BLOOD_PRESSURE_SERVICE]
        )
        for d in found:
            yield DeviceCandidate(transport_id=d.address, name=d.name,
                                  device_kind=DeviceKind.BLOOD_PRESSURE_MONITOR,
                                  services=(BLOOD_PRESSURE_SERVICE,))

    async def read(
        self, candidate: DeviceCandidate, *, timeout_s: float = 120.0
    ) -> AsyncIterator[VitalReading]:
        source = ReadingSource(device_kind=DeviceKind.BLOOD_PRESSURE_MONITOR,
                               device_name=candidate.name,
                               device_id=candidate.transport_id)
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        def on_indication(_char: object, data: bytearray) -> None:
            queue.put_nowait(bytes(data))

        def on_disconnect(_client: object) -> None:
            queue.put_nowait(None)                 # ends the session, does not raise

        async with self._Client(
            candidate.transport_id,
            disconnected_callback=on_disconnect,
            services={BLOOD_PRESSURE_SERVICE},
            timeout=timeout_s,
        ) as client:
            # start_notify covers Indicate too: CoreBluetooth's setNotifyValue: picks
            # notify or indicate from the characteristic's own properties. 0x2A35 is
            # Indicate-only, and this is the call that subscribes to it.
            #
            # The discriminator is macOS-specific: CoreBluetooth delivers reads and
            # notifications through the same delegate callback. We never read 0x2A35,
            # so anything arriving on it is an indication.
            await client.start_notify(
                BLOOD_PRESSURE_MEASUREMENT, on_indication,
                cb={"notification_discriminator": lambda _d: True},
            )
            deadline = asyncio.get_running_loop().time() + timeout_s
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=remaining)
                except TimeoutError:
                    break
                if data is None:                   # disconnected
                    break
                for reading in decode_blood_pressure(data, source):
                    if not reading.is_plausible:
                        continue                   # dropped, and counted in telemetry
                    yield reading


# --------------------------------------------------------------------------------------
# Mock — what the tests use. No radio, no bleak, deterministic.
# --------------------------------------------------------------------------------------


class MockDeviceBackend(DeviceBackend):
    def __init__(self, packets: list[bytes], *, name: str = "MOCK-BP") -> None:
        self._packets, self._name = packets, name

    @property
    def supported_kinds(self) -> frozenset[VitalKind]:
        return frozenset({VitalKind.BLOOD_PRESSURE_SYSTOLIC,
                          VitalKind.BLOOD_PRESSURE_DIASTOLIC, VitalKind.HEART_RATE})

    @property
    def is_standard_profile(self) -> bool:
        return True

    async def discover(self, *, timeout_s: float = 10.0) -> AsyncIterator[DeviceCandidate]:
        yield DeviceCandidate(transport_id="MOCK-0001", name=self._name,
                              device_kind=DeviceKind.BLOOD_PRESSURE_MONITOR,
                              services=(BLOOD_PRESSURE_SERVICE,))

    async def read(
        self, candidate: DeviceCandidate, *, timeout_s: float = 120.0
    ) -> AsyncIterator[VitalReading]:
        source = ReadingSource(device_kind=DeviceKind.BLOOD_PRESSURE_MONITOR,
                               device_name=candidate.name,
                               device_id=candidate.transport_id)
        for pkt in self._packets:
            for reading in decode_blood_pressure(pkt, source):
                if reading.is_plausible:
                    yield reading


# --------------------------------------------------------------------------------------


def _sf(mant: int, exp: int) -> bytes:
    return struct.pack("<H", ((exp & 0xF) << 12) | (mant & 0x0FFF))


async def main() -> int:
    normal = (bytes([0x1E]) + _sf(120, 0) + _sf(80, 0) + _sf(93, 0)
              + struct.pack("<HBBBBB", 2026, 9, 7, 8, 15, 0) + _sf(62, 0)
              + bytes([0xFF]) + struct.pack("<H", 0x0004))
    kpa = bytes([0x05]) + _sf(160, -1) + _sf(107, -1) + _sf(124, -1) + _sf(58, 0)
    minimal = bytes([0x00]) + _sf(118, 0) + _sf(77, 0) + _sf(90, 0)
    cuff_slipped = bytes([0x00]) + _sf(20, 0) + _sf(10, 0) + _sf(13, 0)   # implausible
    clock_unset = (bytes([0x06]) + _sf(131, 0) + _sf(84, 0) + _sf(99, 0)
                   + struct.pack("<HBBBBB", 0, 0, 0, 0, 0, 0) + _sf(70, 0))

    backend = MockDeviceBackend([normal, kpa, minimal, cuff_slipped, clock_unset])
    assert backend.is_standard_profile
    n = 0
    async for cand in backend.discover():
        print(f"candidate: {cand.name} @ {cand.transport_id} kind={cand.device_kind}")
        async for r in backend.read(cand):
            n += 1
            when = datetime.fromtimestamp(r.measured_at_ms / 1000).isoformat(timespec="seconds")
            print(f"  {r.kind.value:<14} {r.value:7.2f} {r.unit:<7} at {when}"
                  f"  note={r.note!r}")
    # 3 (normal) + 3 (kPa) + 2 (minimal, no pulse) + 0 (cuff slipped) + 3 (clock unset)
    expected = 11
    print(f"\n{n} readings, expected {expected}: both halves of the 20/10 packet were "
          f"dropped by PLAUSIBLE ({'ok' if n == expected else 'UNEXPECTED COUNT'})")
    try:
        decode_blood_pressure(bytes([0x00, 0x78, 0x00]), ReadingSource())
    except ValueError as e:
        print(f"short packet correctly rejected: {e}")
    return 0 if n == expected else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
