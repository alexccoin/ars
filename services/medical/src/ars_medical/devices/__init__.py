"""Reading personal medical equipment over BLE, per `research/medical-devices.md`.

`DeviceBackend`/`DeviceCandidate` (the seam, CLAUDE.md rule 2) live in
`ars_core.interfaces` — see that module's docstring for why `DeviceCandidate` is there
rather than in `packages/protocol` for the duration of this change. Everything else
lives here:

  * `MockDeviceBackend` — what every test uses. No radio, no `bleak` import.
  * `BleDeviceBackend` — the only place in A.R.S that imports `bleak`. Blood Pressure
    (`0x1810`) and Heart Rate (`0x180D`) only; see its module docstring for the
    timeout/retry/disconnect contract.
  * `decode_blood_pressure`, `decode_heart_rate` — wired into both backends above via
    `dispatch.decode_indication`.
  * `decode_weight`, `decode_glucose` — decode-only, not connected to a live backend
    (research/medical-devices.md defers both); exist to carry the mandatory unit and
    sentinel guards named in the task brief. See their module docstrings.
  * `VitalStatus` — the closed, structured vocabulary every decoder here writes into
    `VitalReading.status`. No decoder in this package writes into `VitalReading.note`.
  * `PacketDecodeError`, `MeasurementUnsuccessful` — what the decoders raise.
"""

from __future__ import annotations

from .ble_backend import BleDeviceBackend
from .blood_pressure import decode_blood_pressure
from .errors import MeasurementUnsuccessful, PacketDecodeError
from .glucose import GlucoseMeasurement, decode_glucose
from .heart_rate import decode_heart_rate
from .ieee11073 import float32, sfloat
from .mock_backend import MockDeviceBackend
from .status import VitalStatus
from .weight import WeightMeasurement, decode_weight

# `bleak` is NOT imported by the line above: `BleDeviceBackend` only imports it inside
# `__init__`, per CLAUDE.md rule 2. Importing this subpackage is therefore always safe
# for MockDeviceBackend-only callers (every test in this repo) whether or not the
# optional 'bleak' extra (see pyproject.toml) is installed; only *constructing*
# `BleDeviceBackend()` requires it, and raises a clear `ImportError` if it is missing.

__all__ = [
    "BleDeviceBackend",
    "GlucoseMeasurement",
    "MeasurementUnsuccessful",
    "MockDeviceBackend",
    "PacketDecodeError",
    "VitalStatus",
    "WeightMeasurement",
    "decode_blood_pressure",
    "decode_glucose",
    "decode_heart_rate",
    "decode_weight",
    "float32",
    "sfloat",
]
