"""JSON enum equality must not inherit Python's True == 1 coercion."""
import pytest

from codey.toolchain import tool_spec


@pytest.mark.parametrize("schema,value", [
    ({"type": "integer", "enum": [True]}, 1),
    ({"type": "object", "enum": [{"flag": True}]}, {"flag": 1}),
    ({"type": "array", "enum": [[True]]}, [1]),
])
def test_boolean_numeric_collision_does_not_satisfy_enum(schema, value):
    name = "enum_collision_probe"
    assert tool_spec.register_custom_tool(name, grant="control", parameters=(("value", schema),), required=("value",))
    try:
        assert tool_spec.validate_args_against_spec(name, {"value": value})
    finally:
        tool_spec.unregister_custom_tool(name)


def test_number_enum_accepts_equal_json_numbers():
    name = "enum_number_probe"
    assert tool_spec.register_custom_tool(name, grant="control", parameters=(("value", {"type": "number", "enum": [1]}),), required=("value",))
    try:
        assert tool_spec.validate_args_against_spec(name, {"value": 1.0}) == ""
    finally:
        tool_spec.unregister_custom_tool(name)
