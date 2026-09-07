"""IEEE 11073-20601 `medfloat16` (SFLOAT) and `medfloat32` (FLOAT) decode.

The GATT Specification Supplement (2026-02-05, §2.1) renamed these from the older
`SFLOAT`/`FLOAT` names used by every device datasheet and every existing
implementation; this module keeps the old names in comments and identifiers because
that is what a byte capture from a real cuff will be described as everywhere else.

Both types are `mantissa * 10 ** exponent`, both fields two's-complement:

  * `medfloat16` (SFLOAT): 16 bits. Top 4 bits exponent, bottom 12 bits mantissa.
  * `medfloat32` (FLOAT): 32 bits. Top 8 bits exponent, bottom 24 bits mantissa.

`research/experiments/ble/sfloat_check.py` is the oracle for this module — 55
assertions, including the two traps below — and every case there has an equivalent
here. Run it standalone with `uv run python research/experiments/ble/sfloat_check.py`
to re-verify against the specification text directly; this module must never diverge
from it.

**The classic bug** this module exists to not repeat: masking the mantissa first and
comparing *that* to the NaN sentinel. Under that reading, `0x0FFF` — exponent 0,
mantissa -1, i.e. the value **-1.0** — is misread as NaN. The special values below are
therefore always compared against the whole raw word, before any exponent/mantissa
split happens.
"""

from __future__ import annotations

import struct

# IEEE 11073-20601 special values — GSS §2.1.1 Table 2.1, PLXS v1.0.1 Table 5.1.
# Compared against the WHOLE raw word. Never derive these by masking the mantissa.
_SFLOAT_NAN = 0x07FF
_SFLOAT_NRES = 0x0800  # "not at this resolution"
_SFLOAT_RFU = 0x0801
_SFLOAT_PINF = 0x07FE
_SFLOAT_NINF = 0x0802

_FLOAT_NAN = 0x007FFFFF
_FLOAT_NRES = 0x00800000
_FLOAT_RFU = 0x00800001
_FLOAT_PINF = 0x007FFFFE
_FLOAT_NINF = 0x00800002

_SFLOAT_SPECIAL: dict[int, float] = {
    _SFLOAT_NAN: float("nan"),
    _SFLOAT_NRES: float("nan"),
    _SFLOAT_RFU: float("nan"),
    _SFLOAT_PINF: float("inf"),
    _SFLOAT_NINF: float("-inf"),
}
_FLOAT_SPECIAL: dict[int, float] = {
    _FLOAT_NAN: float("nan"),
    _FLOAT_NRES: float("nan"),
    _FLOAT_RFU: float("nan"),
    _FLOAT_PINF: float("inf"),
    _FLOAT_NINF: float("-inf"),
}


def sfloat(raw: int) -> float:
    """`medfloat16` -> float. `raw` is the 16-bit word already read little-endian."""
    special = _SFLOAT_SPECIAL.get(raw)
    if special is not None:
        return special
    exp, mant = raw >> 12, raw & 0x0FFF
    if exp >= 0x8:  # 4-bit two's complement
        exp -= 0x10
    if mant >= 0x800:  # 12-bit two's complement
        mant -= 0x1000
    return mant * (10.0**exp)


def float32(raw: int) -> float:
    """`medfloat32` -> float. `raw` is the 32-bit word already read little-endian."""
    special = _FLOAT_SPECIAL.get(raw)
    if special is not None:
        return special
    exp, mant = raw >> 24, raw & 0x00FFFFFF
    if exp >= 0x80:  # 8-bit two's complement
        exp -= 0x100
    if mant >= 0x800000:  # 24-bit two's complement
        mant -= 0x1000000
    return mant * (10.0**exp)


def read_sfloat(data: bytes, offset: int) -> tuple[float, int]:
    """Reads one little-endian `medfloat16` at `offset`. Returns (value, next_offset)."""
    (raw,) = struct.unpack_from("<H", data, offset)
    return sfloat(raw), offset + 2


def read_float32(data: bytes, offset: int) -> tuple[float, int]:
    """Reads one little-endian `medfloat32` at `offset`. Returns (value, next_offset)."""
    (raw,) = struct.unpack_from("<I", data, offset)
    return float32(raw), offset + 4


__all__ = ["float32", "read_float32", "read_sfloat", "sfloat"]
