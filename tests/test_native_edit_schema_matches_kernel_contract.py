from __future__ import annotations

from codey.toolchain.tool_spec import native_tools_for_policy


class _Policy:
    def allows(self, _name: str) -> bool:
        return True


def test_native_edit_schema_matches_canonical_kernel_replacements() -> None:
    edit = next(
        row["function"]
        for row in native_tools_for_policy(_Policy())
        if row["function"]["name"] == "edit"
    )
    parameters = edit["parameters"]
    properties = parameters["properties"]
    assert set(properties) == {"path", "content", "replacements", "base_result_ref"}
    replacement = properties["replacements"]["items"]
    assert replacement["properties"] == {
        "old_string": {"type": "string"},
        "new_string": {"type": "string"},
    }
    assert replacement["required"] == ["old_string", "new_string"]


def test_edit_repair_explains_how_to_remove_conflicting_base_reference() -> None:
    import pytest

    from codey.toolchain.tool_args_repair import ToolArgsRepairError, normalize_tool_args

    with pytest.raises(ToolArgsRepairError, match="remove the base_result_ref key"):
        normalize_tool_args("edit", {"path": "app.py", "base_result_ref": "read-1",
                                    "replacements": [{"old_string": "5", "new_string": "7"}]})
