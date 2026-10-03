"""Seeded nested schema cases with independently known valid/invalid witnesses."""
from copy import deepcopy
from random import Random

import pytest

from codey.toolchain import tool_spec


@pytest.mark.parametrize("seed", range(20))
def test_nested_schema_accepts_generated_values_and_rejects_single_corruptions(seed):
    rng = Random(seed)
    lo = rng.randrange(-20, 0)
    hi = rng.randrange(1, 20)
    schema = {"type": "object", "properties": {
        "rows": {"type": "array", "items": {"type": "object", "properties": {
            "n": {"type": "integer", "minimum": lo, "maximum": hi},
            "tag": {"type": "string", "enum": ["汉", "x"]},
        }, "required": ["n", "tag"], "additionalProperties": False}},
    }, "required": ["rows"], "additionalProperties": False}
    name = "generated_nested_boundary"
    assert tool_spec.register_custom_tool(name, grant="control", parameters=(("payload", schema),), required=("payload",))
    try:
        for number in [lo, hi, rng.randint(lo, hi)]:
            good = {"payload": {"rows": [{"n": number, "tag": "汉"}]}}
            assert tool_spec.validate_args_against_spec(name, good) == ""
            for bad_number in [lo - 1, hi + 1, True, str(number), number + 0.5]:
                bad = deepcopy(good)
                bad["payload"]["rows"][0]["n"] = bad_number
                assert tool_spec.validate_args_against_spec(name, bad)
            for replacement in [{"n": number}, {"n": number, "tag": "other"}, {"n": number, "tag": "x", "extra": 0}]:
                assert tool_spec.validate_args_against_spec(name, {"payload": {"rows": [replacement]}})
        assert tool_spec.validate_args_against_spec(name, {"payload": {}})
        assert tool_spec.validate_args_against_spec(name, {"payload": {"rows": [], "extra": 0}})
    finally:
        tool_spec.unregister_custom_tool(name)


def test_unsupported_constraint_is_rejected_at_registration():
    assert tool_spec.register_custom_tool("unsupported_pattern", grant="control", parameters=(("x", {"type": "string", "pattern": "x"}),), required=("x",)) is False
