"""`BleDeviceBackend` — the only place in A.R.S permitted to `import bleak`
(CLAUDE.md rule 2). Per the task brief, "do not connect to real hardware" — nothing
here opens a radio. `bleak` itself is never installed for these tests; where its
behaviour matters (disconnect handling), a minimal fake is injected into
`sys.modules` so the actual `ble_backend.py` code path — not a re-description of it —
is what gets exercised.
"""

from __future__ import annotations

import ast
import struct
import sys
from pathlib import Path

import pytest

DEVICES_DIR = Path(__file__).resolve().parents[3] / "services/medical/src/ars_medical/devices"


def _sf(mantissa: int, exponent: int) -> bytes:
    return struct.pack("<H", ((exponent & 0xF) << 12) | (mantissa & 0x0FFF))


_MINIMAL_BP = bytes([0x00]) + _sf(118, 0) + _sf(77, 0) + _sf(90, 0)


def test_bleak_is_imported_only_inside_ble_backend_py():
    """Static check for CLAUDE.md rule 2: no vendor SDK outside a backend
    implementation. Walks every module in `ars_medical.devices` and fails if `bleak`
    is imported anywhere except `ble_backend.py`, and even there only inside a
    function body (never at module scope, so importing the module never requires the
    package to be installed)."""
    offenders: list[str] = []
    for path in DEVICES_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            names = None
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if not names or not any(n == "bleak" or n.startswith("bleak.") for n in names):
                continue
            if path.name != "ble_backend.py":
                offenders.append(f"{path.name}: {ast.dump(node)}")
                continue
            # Inside ble_backend.py: must not be at module (top) scope.
            is_top_level = any(
                node in getattr(module_node, "body", [])
                for module_node in ast.walk(tree)
                if isinstance(module_node, ast.Module)
            )
            if is_top_level:
                offenders.append("ble_backend.py: bleak imported at module scope")
    assert offenders == [], f"bleak imported outside the sanctioned seam: {offenders}"


def test_constructing_without_bleak_installed_raises_an_actionable_error(monkeypatch):
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "bleak" or name.startswith("bleak."):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setitem(sys.modules, "bleak", None)
    monkeypatch.delitem(sys.modules, "bleak.exc", raising=False)
    monkeypatch.setattr("builtins.__import__", fake_import)

    from ars_medical.devices.ble_backend import BleDeviceBackend

    with pytest.raises(ImportError, match=r"ble_backend|bleak|extra"):
        BleDeviceBackend()


# --------------------------------------------------------------------------------
# A minimal fake `bleak`, injected into sys.modules, to exercise the real
# disconnect-handling code path in `ble_backend.py` without a radio.
# --------------------------------------------------------------------------------


class _FakeBleakError(Exception):
    pass


class _FakeClientDeliversThenDisconnects:
    """Simulates a device that indicates once, then drops the link — the normal case
    per research/medical-devices.md §3.5, not a failure."""

    def __init__(self, address, *, disconnected_callback=None, services=None, timeout=None):
        self._disconnected_callback = disconnected_callback

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def start_notify(self, uuid, callback, cb=None):
        callback(None, bytearray(_MINIMAL_BP))
        if self._disconnected_callback is not None:
            self._disconnected_callback(self)


class _FakeClientRaisesDisconnectedMidConnect:
    """Simulates BleakError('disconnected') landing on a pending future between our
    own awaits (research/medical-devices.md §3.5 point 2) rather than through the
    disconnected_callback."""

    def __init__(self, address, *, disconnected_callback=None, services=None, timeout=None):
        pass

    async def __aenter__(self):
        raise _FakeBleakError("disconnected")

    async def __aexit__(self, *exc_info):
        return False


class _FakeClientRaisesUnrelatedError:
    """A genuine failure — must propagate, never be swallowed as "disconnect is
    normal"."""

    def __init__(self, address, *, disconnected_callback=None, services=None, timeout=None):
        pass

    async def __aenter__(self):
        raise _FakeBleakError("GATT operation failed: 0x0e")

    async def __aexit__(self, *exc_info):
        return False


class _FakeScanner:
    @staticmethod
    async def discover(*, timeout, service_uuids):  # noqa: ASYNC109 -- mirrors bleak's own signature
        return []


@pytest.fixture
def fake_bleak(monkeypatch):
    """Installs a fake `bleak` package into `sys.modules` for the duration of one
    test, parameterisable per test via the returned setter."""
    import types

    bleak_module = types.ModuleType("bleak")
    bleak_exc_module = types.ModuleType("bleak.exc")
    bleak_exc_module.BleakError = _FakeBleakError
    bleak_module.exc = bleak_exc_module
    bleak_module.BleakScanner = _FakeScanner

    def _install(client_cls):
        bleak_module.BleakClient = client_cls
        monkeypatch.setitem(sys.modules, "bleak", bleak_module)
        monkeypatch.setitem(sys.modules, "bleak.exc", bleak_exc_module)

    return _install


def _bp_candidate():
    from ars_core import DeviceCandidate
    from ars_protocol import DeviceKind

    return DeviceCandidate(
        transport_id="FAKE-0001", name="fake cuff", device_kind=DeviceKind.BLOOD_PRESSURE_MONITOR
    )


async def test_disconnect_after_delivering_a_reading_ends_cleanly_not_as_an_error(fake_bleak):
    fake_bleak(_FakeClientDeliversThenDisconnects)
    from ars_medical.devices.ble_backend import BleDeviceBackend

    backend = BleDeviceBackend()

    readings = [r async for r in backend.read(_bp_candidate(), timeout_s=1.0)]  # must not raise

    assert len(readings) == 2  # _MINIMAL_BP: systolic + diastolic, no pulse field


async def test_bleak_error_disconnected_on_a_pending_future_ends_cleanly(fake_bleak):
    """research/medical-devices.md §3.5: bleak sets `BleakError("disconnected")` on
    every pending delegate future on disconnect. A backend that lets this propagate
    would report a failure on every session that happens to disconnect before or
    during a connect attempt racing a real drop. It must not raise here."""
    fake_bleak(_FakeClientRaisesDisconnectedMidConnect)
    from ars_medical.devices.ble_backend import BleDeviceBackend

    backend = BleDeviceBackend()

    readings = [r async for r in backend.read(_bp_candidate(), timeout_s=1.0)]  # must not raise

    assert readings == []


async def test_an_unrelated_bleak_error_still_propagates(fake_bleak):
    """The disconnect-is-normal handling must not become "swallow every BleakError" —
    a real GATT failure has to reach the caller as an exception; a fabricated empty
    result would be a worse failure mode than raising."""
    fake_bleak(_FakeClientRaisesUnrelatedError)
    from ars_medical.devices.ble_backend import BleDeviceBackend

    backend = BleDeviceBackend()

    with pytest.raises(Exception, match="GATT operation failed"):
        async for _ in backend.read(_bp_candidate(), timeout_s=1.0):
            pass


async def test_discover_returns_nothing_without_touching_the_fake_client(fake_bleak):
    """No scan implementation detail here opens a real radio; the fake scanner
    returning an empty list is enough to prove `discover()` runs the real code path
    (two sequential per-service scans) without error."""
    fake_bleak(_FakeClientDeliversThenDisconnects)
    from ars_medical.devices.ble_backend import BleDeviceBackend

    backend = BleDeviceBackend()

    candidates = [c async for c in backend.discover(timeout_s=0.01)]

    assert candidates == []


def test_is_standard_profile_and_supported_kinds(fake_bleak):
    fake_bleak(_FakeClientDeliversThenDisconnects)
    from ars_medical.devices.ble_backend import BleDeviceBackend
    from ars_protocol import VitalKind

    backend = BleDeviceBackend()

    assert backend.is_standard_profile is True
    assert backend.supported_kinds == frozenset({
        VitalKind.BLOOD_PRESSURE_SYSTOLIC,
        VitalKind.BLOOD_PRESSURE_DIASTOLIC,
        VitalKind.HEART_RATE,
    })
