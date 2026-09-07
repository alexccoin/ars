"""IEEE 11073-20601 `medfloat16`/`medfloat32` decode — the primitive every other decoder
in `ars_medical.devices` is built on. `research/experiments/ble/sfloat_check.py` is the
oracle (55 assertions, all passing); this file is the same set of assertions run as
real pytest cases against the shipped module, `ars_medical.devices.ieee11073`, rather
than the research script's standalone copy — so a regression here fails CI, not just a
manual re-run.
"""

from __future__ import annotations

import math

import pytest
from ars_medical.devices.ieee11073 import float32, sfloat


def test_positive_integer_exponent_zero():
    assert sfloat(0x0078) == 120.0


def test_negative_exponent_scales_the_mantissa_down():
    """5.5 encoded as mantissa 55, exponent -1 — the shape almost every real BP/glucose
    reading takes, since whole-number mantissas with a small negative exponent are what
    a device transmits for a one-decimal-place reading."""
    assert sfloat(0xF037) == 5.5
    assert sfloat(0xC037) == pytest.approx(0.0055)


def test_negative_mantissa_is_sign_extended():
    assert sfloat(0xFFE1) == -3.1
    assert sfloat(0xF16D) == 36.5


def test_0x0FFF_is_negative_one_not_nan():
    """THE classic bug named in research/medical-devices.md §1.3: masking the mantissa
    first and comparing *that* to the NaN sentinel (0x07FF) misreads 0x0FFF — exponent
    0, mantissa -1 — as NaN. This decoder compares the whole raw word before ever
    splitting it into exponent/mantissa, so this must be -1.0."""
    assert sfloat(0x0FFF) == -1.0


def test_the_other_trap_0xFFFF_is_negative_point_one_not_nan():
    """Same bug, restated at a different exponent: 0xFFFF is exponent -1, mantissa -1,
    i.e. -0.1. research/medical-devices.md §1.3 names this example as "0xF7FF"; by its
    own stated (exponent -1, mantissa -1) that is `(0xF << 12) | 0xFFF == 0xFFFF`, not
    `0xF7FF` (whose mantissa field is `0x7FF`, giving 204.7 — checked, not asserted
    here). Using the raw word the description actually implies rather than the literal
    digits in the doc, since this test's job is to pin the *value*, not transcribe a
    possible typo."""
    assert sfloat(0xFFFF) == -0.1


def test_zero_is_zero_regardless_of_which_zero_exponent_it_carries():
    assert sfloat(0x0000) == 0.0
    assert sfloat(0x8000) == 0.0


def test_sfloat_special_values_compared_against_the_whole_word():
    assert math.isnan(sfloat(0x07FF))  # NaN
    assert math.isnan(sfloat(0x0800))  # NRes ("not at this resolution")
    assert math.isnan(sfloat(0x0801))  # Reserved
    assert sfloat(0x07FE) == float("inf")
    assert sfloat(0x0802) == float("-inf")


def test_float32_decodes_the_same_shape_at_wider_width():
    assert float32(0xFF00016D) == 36.5
    assert float32(0xFF0003D1) == 97.7


def test_float32_special_values():
    assert math.isnan(float32(0x007FFFFF))
    assert math.isnan(float32(0x00800000))
    assert float32(0x007FFFFE) == float("inf")
    assert float32(0x00800002) == float("-inf")
