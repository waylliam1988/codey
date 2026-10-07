"""Ambiguous edits stay atomic and explain how one contextual edit works."""
from __future__ import annotations

import json

import pytest

from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import tools_from_specs
from codey.toolchain.runtime import EditBlock, edit_file


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("split_context", [False, True])
def test_ambiguous_edit_explains_that_context_belongs_in_the_same_replacement(tmp_path, newline, split_context):
    content = "def first():\n    return old_value\n\ndef process():\n    return old_value\n".replace("\n", newline)
    path = tmp_path / "app.py"
    path.write_bytes(content.encode())
    blocks = [EditBlock("    return old_value", "    return new_value")]
    if split_context:
        blocks.insert(0, EditBlock("def process():", "def process():"))
    result = edit_file(tmp_path, "app.py", blocks)
    assert result.ok is False
    assert path.read_bytes() == content.encode()
    assert "same replacement" in result.model_text
    assert "new_string" in result.model_text
    assert "Separate replacement objects" in result.model_text


@pytest.mark.parametrize("native", [False, True])
def test_advertised_multiline_example_changes_only_the_uniquely_anchored_function(tmp_path, native):
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write"})))
    snapshot = build_turn_snapshot(session, native=native)
    spec = next(spec for spec in snapshot.frozen_specs if spec.name == "edit")
    examples = [json.loads(example) for example in spec.json_examples]
    multiline = [example for example in examples if any(
        "\n" in block["old_string"] for block in example["args"].get("replacements", ())
    )]
    assert multiline, "The edit contract needs a complete contextual replacement, not separate anchors."
    example = multiline[0]
    assert example["args"]["path"] == "app.py"
    # Two identical return lines; the example must preserve first() unchanged.
    before = "def first():\n    return old_value\n\ndef process():\n    return old_value\n"
    path = tmp_path / "app.py"
    path.write_text(before, encoding="utf-8")
    plan = normalize_turn(json.dumps(example), snapshot=snapshot)
    assert not plan.protocol_error
    blocks = [EditBlock(row["old_string"], row["new_string"]) for row in plan.calls[0].args["replacements"]]
    result = edit_file(tmp_path, "app.py", blocks)
    assert result.ok is True
    assert path.read_text() == "def first():\n    return old_value\n\ndef process():\n    return new_value\n"
    assert "same replacement" in spec.description
    if native:
        schema = next(tool for tool in tools_from_specs(snapshot.frozen_specs) if tool.name == "edit")
        assert schema.description == spec.description
