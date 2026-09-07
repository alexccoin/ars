"""GSS §3.79 `Date Time`: `uint16` year, `uint8` month, day, hours, minutes, seconds.

No time zone is carried on the wire — it is local wall time on the device
(`research/medical-devices.md` §1.4). Interpreting it in the host's current zone is the
only option available and is wrong across a DST boundary or a trip abroad; there is
nothing this module can do about that, only be honest that it is happening.

`year == 0` (equivalently month or day `== 0`) means "the device's clock is not set" —
GSS defines this explicitly and it is common on a device fresh out of the box or one
that lost power. `decode` returns `None` for that case rather than a bogus 1970 date,
and the caller is expected to timestamp the reading with arrival time and record
`VitalStatus.DEVICE_CLOCK_UNSET` (see `status.py`) instead of silently trusting a
timestamp the device itself does not vouch for.
"""

from __future__ import annotations

import struct
from datetime import datetime


def decode(data: bytes, offset: int) -> tuple[int | None, int]:
    """Reads the 7-octet Date Time field at `offset`. Returns (epoch_ms | None, next_offset)."""
    year, month, day, hour, minute, second = struct.unpack_from("<HBBBBB", data, offset)
    next_offset = offset + 7
    if year == 0 or month == 0 or day == 0:
        return None, next_offset
    try:
        when = datetime(year, month, day, hour, minute, second).astimezone()
    except ValueError:
        # A device sent a syntactically present but calendrically impossible date
        # (e.g. month 13). Treat exactly like "clock unset": do not fabricate a time.
        return None, next_offset
    return int(when.timestamp() * 1000), next_offset


__all__ = ["decode"]
