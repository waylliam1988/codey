from __future__ import annotations

import math

from codey.ghost.numbers import clamp_unit_float, coerce_unit_float


def test_coerce_unit_float_accepts_finite_in_range_numbers() -> None:
    assert coerce_unit_float(0.0) == 0.0
    assert coerce_unit_float(1) == 1.0
    assert coerce_unit_float("0.42") == 0.42
    assert coerce_unit_float(0.123456, digits=6) == 0.123456


def test_coerce_unit_float_rejects_bool_and_unparseable_values() -> None:
    for value in (True, False, None, "high", object(), [0.5]):
        assert coerce_unit_float(value) is None, value


def test_coerce_unit_float_rejects_non_finite_values() -> None:
    # NaN fails every comparison, so range checks alone let it through.
    assert math.isnan(float("nan"))
    assert coerce_unit_float(float("nan")) is None
    assert coerce_unit_float(float("inf")) is None
    assert coerce_unit_float(float("-inf")) is None


def test_coerce_unit_float_rejects_out_of_range_values() -> None:
    assert coerce_unit_float(-0.01) is None
    assert coerce_unit_float(1.01) is None
    assert coerce_unit_float(42.0) is None


def test_clamp_unit_float_defaults_to_zero_for_unusable_input() -> None:
    assert clamp_unit_float(True) == 0.0
    assert clamp_unit_float(None) == 0.0
    assert clamp_unit_float("nope") == 0.0
    assert clamp_unit_float(float("nan")) == 0.0
    assert clamp_unit_float(float("-inf")) == 0.0


def test_clamp_unit_float_clamps_out_of_range_into_unit_interval() -> None:
    assert clamp_unit_float(-3.0) == 0.0
    assert clamp_unit_float(7.5) == 1.0
    assert clamp_unit_float(float("inf")) == 0.0  # non-finite never clamps up
    assert clamp_unit_float(0.98765432, digits=6) == 0.987654
