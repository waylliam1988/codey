"""Coding completion ignores inapplicable metadata, never actual evidence."""
import json

import pytest

from codey.operations.completion_gate import evaluate
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import require_tool_success
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.types.json_value import JsonObject


@pytest.mark.parametrize("kind", ["project", "hybrid"])
@pytest.mark.parametrize("native", [False, True])
def test_coding_contract_and_normalization_share_applicable_fields(kind: str, native: bool) -> None:
    snapshot = restore_snapshot(snapshot_description(build_turn_snapshot(
        TaskSession(TaskPolicy(frozenset({"control"})), task_kind=kind), native=native)))
    done = next(spec for spec in snapshot.model_specs if spec.name == "done")
    assert "source_gaps" not in dict(done.parameters)
    assert "source_gaps" not in snapshot.contract_text
    args: JsonObject = {"summary": "Checked candidate", "source_gaps": [{"reason": "None", "result_ref": "cancelled"}]}
    payload: JsonObject = {"tool": "done", "args": args}
    plan = normalize_turn(AssistantTurn(tool_calls=(ProviderToolCall("done-1", "done", args),)) if native else
        decode_text_turn(json.dumps(payload)), snapshot=snapshot)
    assert not plan.protocol_error
    assert plan.control_args == {"summary": "Checked candidate"}
    assert "source_gaps" in args


@pytest.mark.parametrize("args", [
    {"summary": 5, "source_gaps": []},
    {"summary": "answer", "unknown": True},
    {"summary": "answer", "execution_claims": "invented"},
])
def test_meaningful_argument_errors_are_not_ignored(args: JsonObject) -> None:
    snapshot = build_turn_snapshot(TaskSession(TaskPolicy(frozenset({"control"})), task_kind="project"))
    payload: JsonObject = {"tool": "done", "args": args}
    assert normalize_turn(decode_text_turn(json.dumps(payload)), snapshot=snapshot).protocol_error


def test_ignored_source_metadata_cannot_satisfy_missing_execution_evidence() -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "project.verify"}),
        required_checks=(require_tool_success("run", arguments={"command": "python -m pytest"}),)), task_kind="project")
    snapshot = build_turn_snapshot(session)
    payload: JsonObject = {"tool": "done", "args": {"summary": "Tests passed", "source_gaps": []}}
    plan = normalize_turn(decode_text_turn(json.dumps(payload)), snapshot=snapshot)
    assert not plan.protocol_error
    session.last_done_args = dict(plan.control_args)
    assert not evaluate(session, "Tests passed").complete
