"""Bluetooth SIG assigned 16-bit UUIDs used by this package.

Every 16-bit UUID expands to `0000XXXX-0000-1000-8000-00805f9b34fb` (Bluetooth Base
UUID). Source: Assigned Numbers, 2026-09-04 (`research/medical-devices.md` §1.1/§1.2).

Weight Scale and Glucose UUIDs are included even though no `DeviceBackend` in this
package connects to them yet (`weight.py`/`glucose.py` are decode-only — see their
module docstrings) so the constants exist in one place when that backend is built.
"""

from __future__ import annotations


def _uuid(short: str) -> str:
    return f"0000{short}-0000-1000-8000-00805f9b34fb"


BLOOD_PRESSURE_SERVICE = _uuid("1810")
BLOOD_PRESSURE_MEASUREMENT = _uuid("2a35")
INTERMEDIATE_CUFF_PRESSURE = _uuid("2a36")
"""Inflation telemetry, not a measurement — `research/medical-devices.md` §1.4 says
explicitly not to store this as a reading. Listed here for completeness; no decoder in
this package subscribes to it."""

HEART_RATE_SERVICE = _uuid("180d")
HEART_RATE_MEASUREMENT = _uuid("2a37")
BODY_SENSOR_LOCATION = _uuid("2a38")

WEIGHT_SCALE_SERVICE = _uuid("181d")
WEIGHT_MEASUREMENT = _uuid("2a9d")

GLUCOSE_SERVICE = _uuid("1808")
GLUCOSE_MEASUREMENT = _uuid("2a18")
RECORD_ACCESS_CONTROL_POINT = _uuid("2a52")

KPA_TO_MMHG = 7.50061682704
LB_TO_KG = 0.45359237

__all__ = [
    "BLOOD_PRESSURE_MEASUREMENT",
    "BLOOD_PRESSURE_SERVICE",
    "BODY_SENSOR_LOCATION",
    "GLUCOSE_MEASUREMENT",
    "GLUCOSE_SERVICE",
    "HEART_RATE_MEASUREMENT",
    "HEART_RATE_SERVICE",
    "INTERMEDIATE_CUFF_PRESSURE",
    "KPA_TO_MMHG",
    "LB_TO_KG",
    "RECORD_ACCESS_CONTROL_POINT",
    "WEIGHT_MEASUREMENT",
    "WEIGHT_SCALE_SERVICE",
]
