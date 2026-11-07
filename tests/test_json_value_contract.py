"""Strict JSON and legacy transport encoding have different admission rules."""

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

import pytest

from codey.providers.response_record import json_value
from codey.runtime.core.models import canonical_json_mapping
from codey.types.json_value import JsonLimits, decode_json_object, decode_json_value, require_json_string


@pytest.mark.parametrize("value", [float("nan"), float("inf"), {1: "bad"}, {"x": object()}, "\ud800"])
def test_non_json_values_are_rejected_at_both_boundaries(value):
    with pytest.raises(ValueError):
        decode_json_value(value)
    with pytest.raises(ValueError):
        json_value(value)


def test_decoder_detaches_shared_containers_without_mutating_input():
    child = [1, False, None, "中文😀"]
    original = {"left": child, "right": child}
    decoded = decode_json_object(original)
    child.append("late mutation")
    assert decoded == {"left": [1, False, None, "中文😀"], "right": [1, False, None, "中文😀"]}
    assert decoded["left"] is not decoded["right"]


def test_cycles_fail_with_a_controlled_error():
    cycle = []
    cycle.append(cycle)
    for decode in (decode_json_value, json_value):
        with pytest.raises(ValueError, match="cycle"):
            decode(cycle)


@pytest.mark.parametrize("value,limits,match", [
    ([[[1]]], JsonLimits(max_depth=2), "depth"),
    ([1, 2, 3], JsonLimits(max_nodes=3), "node"),
    ({"中": "😀"}, JsonLimits(max_string_bytes=6), "byte"),
])
def test_resource_limits_fail_without_clipping(value, limits, match):
    with pytest.raises(ValueError, match=match):
        decode_json_value(value, limits=limits)


def test_legacy_python_transport_structures_are_not_external_json():
    @dataclass(frozen=True)
    class Record:
        name: str
        positions: tuple[int, ...]

    class Kind(Enum):
        READ = "read"

    record = Record("中", (1, 2))
    assert json_value(record) == {"name": "中", "positions": [1, 2]}
    assert json_value({"kind": Kind.READ, "set": frozenset({2, 1})}) == {"kind": "read", "set": [1, 2]}
    for value in (record, (1, 2), frozenset({1}), Kind.READ):
        with pytest.raises(ValueError):
            decode_json_value(value)
    assert canonical_json_mapping(MappingProxyType({"positions": (1, 2)})) == {"positions": [1, 2]}
    with pytest.raises(TypeError):
        canonical_json_mapping({"positions": {1, 2}})


def test_explicit_field_validation_does_not_coerce_values():
    assert require_json_string("", "summary") == ""
    with pytest.raises(ValueError, match="summary"):
        require_json_string(12, "summary")
    with pytest.raises(ValueError, match="object"):
        decode_json_object([])
