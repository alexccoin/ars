"""`BleDeviceBackend` — the only place in `ars_medical` (and, per CLAUDE.md rule 2, in
all of A.R.S) permitted to `import bleak`. Speaks the two adopted Bluetooth SIG GATT
profiles this package supports: Blood Pressure (`0x1810`) and Heart Rate (`0x180D`).

Nothing in this module is exercised by the test suite against a real radio — see the
task brief and `research/medical-devices.md` for why ("do not connect to real
hardware... no scan, no pairing, no connection"). What *is* tested here, without
importing `bleak` at all, is that `bleak` is never imported anywhere in this package
except from inside `BleDeviceBackend.__init__` (`tests/unit/medical/
test_devices_ble_backend.py::test_bleak_is_only_imported_here`), and that constructing
this class without `bleak` installed fails with an actionable message rather than a
bare `ModuleNotFoundError` three frames deep in a vendor package.

**Timeout, retry, and failure behaviour — stated here, not just in the PR:**

  * `discover`/`read` accept `timeout_s` and pass it straight to `bleak`; there is no
    internal retry loop. `research/medical-devices.md` §3.5: bleak does not
    auto-reconnect and nothing in the CoreBluetooth backend retries a dropped
    connection. Retrying here would mean silently reopening the Bluetooth radio without
    a fresh, explicit call from whatever asked for a reading — the caller (the skill
    wrapping this backend) decides whether to try again, this backend never does.
  * A **normal disconnect** — the device dropping the link after delivering a reading,
    which several of these devices do deliberately (PLXS §3.1.1, and observed on the
    Blood Pressure and Heart Rate profiles too) — ends `read`'s generator without
    raising. This is the whole reason `on_disconnect` pushes a sentinel onto a queue
    instead of being left to bleak's default behaviour, which is to raise
    `BleakError("disconnected")` on every pending delegate future
    (`research/medical-devices.md` §3.5 point 2). This backend also catches that
    specific `BleakError` around the connection body as a second line of defence, for
    the disconnect that lands *between* our own awaits rather than inside a queue read.
  * A genuine **failure to connect** (device unreachable, pairing rejected, GATT error
    unrelated to disconnection) propagates as an exception. There is no fallback value
    — a fabricated "connection failed, here is an empty reading" is a worse failure
    mode than an exception the caller has to handle.
  * A **single malformed or explicitly-unsuccessful indication** does not end the
    session — see `dispatch.decode_indication`. Only a real disconnect or the deadline
    ends the generator.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from ars_core import DeviceBackend, DeviceCandidate
from ars_protocol import DeviceKind, ReadingSource, VitalKind, VitalReading
from ars_telemetry import aspan, counter, histogram

from . import gatt
from .dispatch import decode_indication

_SERVICE_BY_KIND: dict[DeviceKind, tuple[str, str]] = {
    # device_kind -> (service uuid, measurement characteristic uuid)
    DeviceKind.BLOOD_PRESSURE_MONITOR: (
        gatt.BLOOD_PRESSURE_SERVICE, gatt.BLOOD_PRESSURE_MEASUREMENT
    ),
    DeviceKind.HEART_RATE_MONITOR: (gatt.HEART_RATE_SERVICE, gatt.HEART_RATE_MEASUREMENT),
}

_INSTALL_HINT = (
    "BleDeviceBackend requires the optional 'bleak' dependency. Install it with "
    "`uv sync --extra ble` (see services/medical/pyproject.toml) before constructing "
    "this backend. MockDeviceBackend needs none of this and is what tests use."
)


class BleDeviceBackend(DeviceBackend):
    def __init__(self) -> None:
        try:
            from bleak import BleakClient, BleakScanner
            from bleak.exc import BleakError
        except ImportError as exc:  # pragma: no cover - exercised only without bleak installed
            raise ImportError(_INSTALL_HINT) from exc
        self._Client = BleakClient
        self._Scanner = BleakScanner
        self._BleakError = BleakError

    @property
    def supported_kinds(self) -> frozenset[VitalKind]:
        return frozenset(
            {
                VitalKind.BLOOD_PRESSURE_SYSTOLIC,
                VitalKind.BLOOD_PRESSURE_DIASTOLIC,
                VitalKind.HEART_RATE,
            }
        )

    @property
    def is_standard_profile(self) -> bool:
        return True

    async def discover(self, *, timeout_s: float = 10.0) -> AsyncIterator[DeviceCandidate]:
        # service_uuids is not just an optimisation on macOS: 12.0-12.2 return no
        # advertisement data at all without it (research/medical-devices.md §3.4), and
        # filtering keeps every other BLE device in the building out of the log.
        #
        # Run as two sequential single-service scans rather than one filtered on both
        # UUIDs: bleak's own `service_uuids` filter is OR-matched by the OS, which
        # would work, but it leaves no reliable way to recover *which* of the two
        # services a match advertised without depending on `AdvertisementData` shapes
        # this module was never able to verify against a real radio (rule 6). Two
        # scans of `timeout_s` each is the wall-time cost of not guessing at that API.
        for device_kind, (service_uuid, _measurement_uuid) in _SERVICE_BY_KIND.items():
            async with aspan(
                "medical.devices.ble_discover", device_kind=device_kind.value, timeout_s=timeout_s
            ):
                found = await self._Scanner.discover(
                    timeout=timeout_s, service_uuids=[service_uuid]
                )
                for device in found:
                    counter("ars_medical.devices.candidate_found").add(
                        1, device_kind=device_kind.value
                    )
                    yield DeviceCandidate(
                        transport_id=device.address,
                        name=device.name,
                        device_kind=device_kind,
                        services=(service_uuid,),
                    )

    async def read(
        self, candidate: DeviceCandidate, *, timeout_s: float = 120.0
    ) -> AsyncIterator[VitalReading]:
        service_char = _SERVICE_BY_KIND.get(candidate.device_kind)
        if service_char is None:
            raise ValueError(
                f"BleDeviceBackend cannot read device_kind={candidate.device_kind.value!r}"
            )
        service_uuid, measurement_uuid = service_char
        source = ReadingSource(
            device_kind=candidate.device_kind,
            device_name=candidate.name,
            device_id=candidate.transport_id,
        )

        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        def on_indication(_char: object, data: bytearray) -> None:
            queue.put_nowait(bytes(data))

        def on_disconnect(_client: object) -> None:
            counter("ars_medical.devices.disconnect").add(
                1, device_kind=candidate.device_kind.value
            )
            queue.put_nowait(None)  # ends the session below; never raised as an error

        first_reading_at: float | None = None
        start = asyncio.get_running_loop().time()

        async with aspan(
            "medical.devices.ble_read", device_kind=candidate.device_kind.value, timeout_s=timeout_s
        ):
            try:
                async with self._Client(
                    candidate.transport_id,
                    disconnected_callback=on_disconnect,
                    services={service_uuid},
                    timeout=timeout_s,
                ) as client:
                    # start_notify covers Indicate too: CoreBluetooth's setNotifyValue:
                    # selects notify or indicate from the characteristic's own declared
                    # properties (research/medical-devices.md §3.1). We never read
                    # either measurement characteristic, so the discriminator below is
                    # exactly right: anything arriving on it is a notification or
                    # indication, never a read response.
                    await client.start_notify(
                        measurement_uuid,
                        on_indication,
                        cb={"notification_discriminator": lambda _d: True},
                    )
                    deadline = start + timeout_s
                    while True:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            break
                        try:
                            data = await asyncio.wait_for(queue.get(), timeout=remaining)
                        except TimeoutError:
                            break
                        if data is None:  # disconnected — normal, not an error
                            break
                        for reading in decode_indication(candidate.device_kind, data, source):
                            if first_reading_at is None:
                                first_reading_at = asyncio.get_running_loop().time()
                                histogram("ars_medical.devices.time_to_first_reading_s").record(
                                    first_reading_at - start,
                                    device_kind=candidate.device_kind.value,
                                )
                            yield reading
            except self._BleakError as exc:
                if "disconnect" not in str(exc).lower():
                    raise  # a real connection/GATT failure, not the disconnect-is-normal case
                counter("ars_medical.devices.disconnect").add(
                    1, device_kind=candidate.device_kind.value
                )


__all__ = ["BleDeviceBackend"]
