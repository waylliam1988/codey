from codey.toolchain.tool_spec import (
    register_custom_tool,
    unregister_custom_tool,
    validate_args_against_spec,
)


def test_integer_array_rejects_non_array_and_wrong_items() -> None:
    name = "test_integer_array"
    assert register_custom_tool(
        name,
        grant="control",
        parameters=(("items", {"type": "array", "items": {"type": "integer"}}),),
        required=("items",),
    )
    try:
        assert validate_args_against_spec(name, {"items": [1, 2]}) == ""
        assert validate_args_against_spec(name, {"items": ["wrong"]})
        assert validate_args_against_spec(name, {"items": {"wrong": "object"}})
        assert validate_args_against_spec(name, {"items": "wrong-string"})
    finally:
        unregister_custom_tool(name)
