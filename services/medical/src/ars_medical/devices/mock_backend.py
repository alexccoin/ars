"""`MockDeviceBackend` — what every test in this package uses. No radio, no `bleak`
import, deterministic: it replays a fixed list of raw characteristic payloads that the
caller constructed by hand (or copied from `research/medical-devices.md`'s recorded
byte sequences). Per the task brief: "do not connect to real hardware... everything is
tested against the mock backend and against recorded byte payloads."
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence

from ars_core import DeviceBackend, DeviceCandidate
from ars_protocol import DeviceKind, ReadingSource, VitalKind, VitalReading
from ars_telemetry import aspan

from .dispatch import decode_indication

_SUPPORTED_KINDS: dict[DeviceKind, frozenset[VitalKind]] = {
    DeviceKind.BLOOD_PRESSURE_MONITOR: frozenset({
        VitalKind.BLOOD_PRESSURE_SYSTOLIC,
        VitalKind.BLOOD_PRESSURE_DIASTOLIC,
        VitalKind.HEART_RATE,
    }),
    DeviceKind.HEART_RATE_MONITOR: frozenset({VitalKind.HEART_RATE}),
}


class MockDeviceBackend(DeviceBackend):
    """Replays `packets` as if they were indications/notifications from one candidate
    of `device_kind`. `disconnect_after` truncates the replay early to simulate a
    device that drops the link partway through — the normal case, not an error (see
    `DeviceBackend.read`'s docstring); this backend still ends its generator cleanly.
    """

    def __init__(
        self,
        device_kind: DeviceKind,
        packets: Sequence[bytes],
        *,
        name: str = "MOCK",
        transport_id: str = "MOCK-0001",
        disconnect_after: int | None = None,
    ) -> None:
        if device_kind not in _SUPPORTED_KINDS:
            raise ValueError(f"MockDeviceBackend has no decoder for {device_kind.value!r}")
        self._device_kind = device_kind
        self._packets = tuple(packets)
        self._name = name
        self._transport_id = transport_id
        self._disconnect_after = disconnect_after

    @property
    def supported_kinds(self) -> frozenset[VitalKind]:
        return _SUPPORTED_KINDS[self._device_kind]

    @property
    def is_standard_profile(self) -> bool:
        return True

    async def discover(self, *, timeout_s: float = 10.0) -> AsyncIterator[DeviceCandidate]:
        await asyncio.sleep(0)  # stays a real coroutine checkpoint, same shape as bleak's
        yield DeviceCandidate(
            transport_id=self._transport_id, name=self._name, device_kind=self._device_kind
        )

    async def read(
        self, candidate: DeviceCandidate, *, timeout_s: float = 120.0
    ) -> AsyncIterator[VitalReading]:
        source = ReadingSource(
            device_kind=self._device_kind,
            device_name=candidate.name,
            device_id=candidate.transport_id,
        )
        async with aspan("medical.devices.mock_read", device_kind=self._device_kind.value):
            packets = self._packets
            if self._disconnect_after is not None:
                packets = packets[: self._disconnect_after]
            for packet in packets:
                await asyncio.sleep(0)
                for reading in decode_indication(self._device_kind, packet, source):
                    yield reading


__all__ = ["MockDeviceBackend"]
