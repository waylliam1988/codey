"""Direct exposure and nested execution share definitions and authorization."""

import copy

import pytest

from codey.codemode.kernel import build_execute_turn_executor
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.runtime.core.models import ToolCall
from tests.test_codemode_replay_authority import fixture


def test_hidden_read_uses_original_parent_effect_but_cannot_be_called_directly(tmp_path):
    project, _, _, mutations, outputs, revisions, sink, session = fixture(tmp_path)
    session.model_tool_names = ("codemode", "done")
    snapshot = build_turn_snapshot(session, native=True)
    assert snapshot.tool_names == ("codemode", "done")
    assert "read_file" in snapshot.nested_tool_names
    assert "read_file" not in {spec.name for spec in snapshot.model_specs}
    assert "read_file(args:" in next(spec.declarations for spec in snapshot.frozen_specs if spec.name == "codemode")
    serialized = snapshot_description(snapshot)
    restored = restore_snapshot(serialized)
    assert restored.tool_names == snapshot.tool_names
    assert restored.nested_tool_names == snapshot.nested_tool_names
    native = AssistantTurn(tool_calls=(ProviderToolCall("direct", "read_file", {"path": "a.py"}),))
    rejected_plan = normalize_turn(native, snapshot=restored)
    assert rejected_plan.protocol_error_kind == "snapshot_tool_mismatch"
    from codey.operations.kernel_prompt import _repair_prompt

    repair = _repair_prompt(rejected_plan, snapshot=restored, native=True)
    assert "Correct read_file" not in repair and "Available tools: codemode, done" in repair
    try:
        call = ToolCall("read_file", {"path": "a.py"})
        rejected = execute_turn(session, [call], snapshot=restored, intent_sink=sink, durable=mutations.durable)[0]
        assert not rejected.ok
        assert not mutations.durable.effects("r")
        rejected = execute_turn(session, [call], snapshot=restored, intent_sink=sink,
                                durable=mutations.durable, nested=True)[0]
        assert not rejected.ok and "admitted parent" in rejected.model_text
        parent = build_execute_turn_executor(session=session, snapshot=restored, run_id="r", turn=0,
            effect_scope="outer", project_path=project, managed_outputs=outputs, workspace_revision_store=revisions,
            intent_sink=sink, durable=mutations.durable, session_id="s")
        result = execute_turn(session, [ToolCall("codemode", {"code": "return await tools.read_file({path:'a.py'});"})],
            executors={"codemode": parent}, snapshot=restored, intent_sink=sink, durable=mutations.durable,
            run_id="r", effect_scope="outer", turn=0, project_path=project, managed_outputs=outputs,
            workspace_revision_store=revisions)[0]
        assert result.ok, result.model_text
        effects = mutations.durable.effects("r")
        child = next(row for row in effects if row.intent.get("tool_name") == "read")
        assert child.intent["parent_effect_id"] and child.settlement is not None
        forged = copy.deepcopy(serialized)
        forged["nested_tool_names"].append("done")
        with pytest.raises(ValueError, match="nested capabilities"):
            restore_snapshot(forged)
    finally:
        mutations.durable.close()


def test_exposure_filter_cannot_add_an_unauthorized_tool():
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy

    session = TaskSession(TaskPolicy(frozenset({"control"})), model_tool_names=("done", "edit"))
    with pytest.raises(ValueError, match="narrow"):
        build_turn_snapshot(session)
