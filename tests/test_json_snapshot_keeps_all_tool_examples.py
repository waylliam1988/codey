"""The web/text protocol must show both create and existing-file edit shapes."""
from __future__ import annotations

import json

import pytest

from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.toolchain.definition import TOOL_DEFINITIONS


@pytest.mark.parametrize("native", [False, True])
def test_frozen_turn_contract_shows_every_declared_edit_shape(native):
    session = TaskSession(policy=TaskPolicy(frozenset({"control", "project.read", "project.write"})))
    snapshot = build_turn_snapshot(session, native=native)
    definition = next(item for item in TOOL_DEFINITIONS if item.name == "edit")
    shapes = [json.loads(example)["args"] for example in definition.examples]
    assert any("content" in args for args in shapes)
    assert any(len(args.get("replacements", [])) == 1 for args in shapes)
    assert any(len(args.get("replacements", [])) > 1 for args in shapes)
    for example in definition.examples:
        assert example in snapshot.contract_text, "snapshot dropped an existing-file edit example"
        plan = normalize_turn(example, snapshot=snapshot)
        assert not plan.protocol_error
        assert plan.calls[0].name == "edit"
        assert plan.calls[0].args == json.loads(example)["args"]
