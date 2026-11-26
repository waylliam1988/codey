"""JSON enum equality must not inherit Python's True == 1 coercion."""
import pytest

from codey.toolchain import tool_spec
from codey.toolchain.result_schema import schema_typescript
from codey.types.json_value import JsonObject, JsonValue, canonical_json_object


@pytest.mark.parametrize("schema,value", [
    ({"type": "integer", "enum": [True]}, 1),
    ({"type": "object", "enum": [{"flag": True}]}, {"flag": 1}),
    ({"type": "array", "enum": [[True]]}, [1]),
])
def test_boolean_numeric_collision_does_not_satisfy_enum(schema: JsonObject, value: JsonValue) -> None:
    name = "enum_collision_probe"
    assert tool_spec.register_custom_tool(name, grant="control", parameters=(("value", schema),), required=("value",))
    try:
        assert tool_spec.validate_args_against_spec(name, {"value": value})
    finally:
        tool_spec.unregister_custom_tool(name)


def test_number_enum_accepts_equal_json_numbers() -> None:
    name = "enum_number_probe"
    assert tool_spec.register_custom_tool(name, grant="control", parameters=(("value", {"type": "number", "enum": [1]}),), required=("value",))
    try:
        assert tool_spec.validate_args_against_spec(name, {"value": 1.0}) == ""
    finally:
        tool_spec.unregister_custom_tool(name)


@pytest.mark.parametrize("value,accepted", [
    ({"role": "textbox", "flags": [True, 1]}, True),
    ({"role": "textbox", "flags": [1, True]}, False),
    ({"role": "button", "flags": [True, 1]}, False),
])
def test_object_enum_preserves_json_identity_after_freezing(value: JsonObject, accepted: bool) -> None:
    schema: JsonObject = {"type": "object", "enum": [{"role": "textbox", "flags": [True, 1]}]}
    original = tool_spec.ToolSpec(name="enum_probe", parameters=(("value", schema),), required=("value",))
    frozen = tool_spec.freeze_spec_parameters(original)
    assert bool(tool_spec.validate_args_with_spec(frozen, {"value": value})) is (not accepted)
    assert schema_typescript(canonical_json_object(dict(frozen.parameters)["value"])) == (
        '{"flags": [true, 1], "role": "textbox"}')
