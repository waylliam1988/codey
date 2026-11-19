from __future__ import annotations

from codey.toolchain.tool_spec import native_tools_for_policy, spec_for_tool
from codey.types.json_value import decode_json_object, parse_json_object


class _Policy:
    def allows(self, grant: str) -> bool:
        return True


def test_native_edit_schema_matches_canonical_kernel_replacements() -> None:
    edit = next(
        decode_json_object(row["function"])
        for row in native_tools_for_policy(_Policy())
        if decode_json_object(row["function"])["name"] == "edit"
    )
    parameters = decode_json_object(edit["parameters"])
    properties = decode_json_object(parameters["properties"])
    assert set(properties) == {"path", "content", "replacements", "base_result_ref",
                               "region", "relative_indent"}
    replacement = decode_json_object(decode_json_object(properties["replacements"])["items"])
    assert replacement["properties"] == {
        "old_string": {"type": "string"},
        "new_string": {"type": "string"},
    }
    assert replacement["required"] == ["old_string", "new_string"]


def test_primary_edit_example_uses_the_shared_region_contract() -> None:
    from codey.operations.kernel_tool_args import _validate_tool_args

    spec = spec_for_tool("edit")
    assert spec is not None and spec.description.startswith("Edit a local Python region")
    example = parse_json_object(spec.json_examples[0])
    normalized, error = _validate_tool_args("edit", decode_json_object(example["args"]))
    assert not error
    assert normalized["relative_indent"] is True and normalized["region"] == "process"


def test_edit_repair_explains_how_to_remove_conflicting_base_reference() -> None:
    import pytest

    from codey.toolchain.tool_args_repair import ToolArgsRepairError, normalize_tool_args

    with pytest.raises(ToolArgsRepairError, match="remove the base_result_ref key"):
        normalize_tool_args("edit", {"path": "app.py", "base_result_ref": "read-1",
                                    "replacements": [{"old_string": "5", "new_string": "7"}]})
