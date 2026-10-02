"""No alternate edit shape survives in the argument normalizer."""

import pytest

from codey.toolchain.tool_args_repair import ToolArgsRepairError, normalize_tool_args


@pytest.mark.parametrize("args", [
    {"path": "a.py", "old_string": "a", "new_string": "b"},
    {"path": "a.py", "replacements": '{"old_string":"a","new_string":"b"}'},
    {"path": "a.py", "replacements": {"old_string": "a", "new_string": "b"}},
])
def test_edit_normalizer_rejects_shapes_absent_from_schema(args):
    with pytest.raises(ToolArgsRepairError):
        normalize_tool_args("edit", args)


def test_canonical_deletion_requires_no_repair():
    args = {"path": "a.py", "replacements": [{"old_string": "a", "new_string": ""}]}
    result = normalize_tool_args("edit", args)
    assert result.args == args
    assert result.arg_repair_counts == {}
    assert result.alias_rewrite_count == 0
