"""`decode_glucose` — `0x2A18`, GLS 1.0.1 §3.1.1.5. This file is the regression test
for the third named bug in the task brief: a glucose SFLOAT decoded without applying
the mandatory kg/L<->mg/dL or mol/L<->mmol/L pipeline can produce a number that is
*also* inside `PLAUSIBLE[VitalKind.BLOOD_GLUCOSE]` (1.0-40.0 mmol/L) — silently wrong,
not caught by the range check. `decode_glucose` is not wired to any live
`DeviceBackend` — see its module docstring — but the guard has to exist and be correct
regardless of what calls it.

Byte layouts match `research/experiments/ble/sfloat_check.py`'s own `decode_glucose`
test vectors (`g1`, `g2`, and the "naive mantissa" comment in its glucose section).
"""

from __future__ import annotations

import struct

import pytest
from ars_medical.devices.errors import PacketDecodeError
from ars_medical.devices.glucose import decode_glucose
from ars_protocol import PLAUSIBLE, VitalKind


def _sf(mantissa: int, exponent: int) -> bytes:
    return struct.pack("<H", ((exponent & 0xF) << 12) | (mantissa & 0x0FFF))


def _base(flags: int, seq: int = 1) -> bytes:
    return bytes([flags]) + struct.pack("<H", seq) + struct.pack("<HBBBBB", 2026, 9, 7, 8, 0, 0)


def test_mg_dl_device_bit2_clear_converts_through_the_kg_per_l_pipeline():
    """100 mg/dL device: base unit kg/L, encoded as mantissa 100, exponent -5
    (GLS §3.1.1.5's own worked example). Correct pipeline: mg/dL = value * 1e5, then
    mmol/L = mg/dL / 18.0156 — never the raw decoded SFLOAT treated as the final unit."""
    packet = _base(0x02) + _sf(100, -5) + bytes([0x11])  # concentration present, bit2=0

    reading = decode_glucose(packet)

    assert round(reading.mmol_l, 3) == 5.551  # matches sfloat_check.py's g1 oracle


def test_mmol_l_device_bit2_set_converts_through_the_mol_per_l_pipeline():
    """5.5 mmol/L device: base unit mol/L, encoded as mantissa 55, exponent -4.
    Correct pipeline: mmol/L = value * 1e3."""
    packet = _base(0x02 | 0x04) + _sf(55, -4) + bytes([0x11])  # bit2=1

    reading = decode_glucose(packet)

    assert reading.mmol_l == pytest.approx(5.5)


def test_bug_named_in_the_brief__naive_mantissa_read_is_plausible_and_ten_times_wrong():
    """research/medical-devices.md §1.6, quoted directly: "a device reporting 3.0
    mmol/L gives mantissa 30 — inside the range, silently wrong by 10x." This is the
    concrete byte-level case the brief's "mmol/L read as mg/dL" bug describes: a
    decoder that reads the SFLOAT's raw mantissa bits (30) instead of running the
    exponent through the unit-bit pipeline (which yields the true value, 3.0) produces
    a number that is *also* inside PLAUSIBLE (1.0-40.0) — so nothing downstream would
    ever notice. `decode_glucose` must produce 3.0, never 30."""
    low, high = PLAUSIBLE[VitalKind.BLOOD_GLUCOSE]
    true_mmol_l = 3.0
    naive_mantissa_only = 30  # what you get by masking 0x0FFF and ignoring the exponent
    assert low <= true_mmol_l <= high
    assert low <= naive_mantissa_only <= high, "the whole point: also plausible, 10x off"

    packet = _base(0x02 | 0x04) + _sf(30, -4) + bytes([0x11])  # true value: 30 * 10^-4 mol/L

    reading = decode_glucose(packet)

    assert reading.mmol_l == pytest.approx(true_mmol_l)
    assert reading.mmol_l != pytest.approx(naive_mantissa_only)


def test_same_raw_sfloat_bits_mean_different_things_under_each_unit_branch():
    """The units bit selects which pipeline runs, not just a display label — decoding
    the identical raw SFLOAT word under the wrong branch produces a different,
    plausible-looking number rather than an error, which is exactly why this module's
    guard is "always branch on the actual flag bit", never a hardcoded assumption."""
    raw = _sf(55, -4)  # 0.0055, correct as mol/L (5.5 mmol/L)

    as_mol_l = decode_glucose(_base(0x02 | 0x04) + raw + bytes([0x11])).mmol_l
    as_kg_l = decode_glucose(_base(0x02) + raw + bytes([0x11])).mmol_l

    assert as_mol_l == pytest.approx(5.5)
    assert as_kg_l != pytest.approx(as_mol_l)


def test_concentration_absent_is_none_not_a_fabricated_zero():
    packet = _base(0x00)  # bit1 clear: no concentration field at all
    reading = decode_glucose(packet)
    assert reading.mmol_l is None


def test_device_clock_unset_is_flagged():
    packet = bytes([0x00]) + struct.pack("<H", 1) + struct.pack("<HBBBBB", 0, 0, 0, 0, 0, 0)
    reading = decode_glucose(packet)
    assert reading.clock_unset is True
    assert reading.base_time_ms is None


def test_a_short_packet_raises():
    with pytest.raises(PacketDecodeError):
        decode_glucose(bytes([0x00, 0x01, 0x00]))
