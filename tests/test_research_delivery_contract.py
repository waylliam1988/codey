"""A submitted report owns citations; delivery does not ask for a second copy."""
from __future__ import annotations

import pytest

from codey.knowledge.model import canonical_json
from codey.operations.execution_claims import observed_claim, validate_execution_claims
from codey.operations.kernel_prompt import _completion_followup
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.runtime.core.execution_receipt import ExecutionReceipt
from codey.runtime.core.models import ToolCall, ToolResult
from codey.toolchain.tool_spec import thaw_schema_value
from codey.types.json_value import decode_json_object, parse_json_object


def delivery_session() -> TaskSession:
    return TaskSession(TaskPolicy(frozenset({"control", "web.read", "knowledge.read"}),
        strict_research=True, required_checks=("research_submission",)), task_kind="hybrid")


def test_report_delivery_does_not_request_duplicate_knowledge_identities() -> None:
    snapshot = build_turn_snapshot(delivery_session())
    done = next(spec for spec in snapshot.model_specs if spec.name == "done")
    assert "knowledge_refs" not in dict(done.parameters)
    assert "summary" in done.required
    assert "submit_research_answer" in done.description
    assert "knowledge_refs" not in decode_json_object(parse_json_object(done.json_examples[0])["args"])
    valid = normalize_turn(decode_text_turn('{"tool":"done","args":{"summary":"Verified changes."}}'), snapshot=snapshot)
    assert valid.control is not None and valid.control.kind == "done"
    invalid = normalize_turn(decode_text_turn('{"tool":"done","args":{"summary":"Verified changes.",'
        '"knowledge_refs":[{"id":"e1","revision":1}]}}'), snapshot=snapshot)
    assert invalid.protocol_error and "knowledge_refs" in invalid.protocol_error


@pytest.mark.parametrize("native", [False, True])
def test_report_repair_is_actionable_in_both_transports(native: bool) -> None:
    feedback = _completion_followup("Research submission is missing.", native=native, whole_submission=True)
    assert "submit_research_answer" in feedback
    assert "Keep a settled valid report" in feedback
    assert "then propose done with summary" in feedback
    assert "Do not use [eN] labels as knowledge_refs" in feedback


def test_ordinary_saved_knowledge_completion_keeps_its_contract() -> None:
    session = delivery_session()
    session.policy = TaskPolicy(session.policy.grants)
    done = next(spec for spec in build_turn_snapshot(session).model_specs if spec.name == "done")
    assert "knowledge_refs" in dict(done.parameters)


def test_execution_claim_choices_derive_from_original_receipts() -> None:
    session = delivery_session()
    receipt = ExecutionReceipt("exit", 0, "checks passed", exit_code_scope="process")
    session._memory_results["checked"] = ToolResult(ToolCall("run", {"command": "check"}), "checked", ok=True,
        canonical={"execution": receipt.facts()})
    snapshot = build_turn_snapshot(session)
    done = next(spec for spec in snapshot.model_specs if spec.name == "done")
    schema = decode_json_object(thaw_schema_value(dict(done.parameters)["execution_claims"]))
    claim = observed_claim("checked", receipt)
    assert decode_json_object(schema["items"])["enum"] == [claim]
    restored = restore_snapshot(parse_json_object(canonical_json(snapshot_description(snapshot))))
    assert restored == snapshot
    valid_args = {"summary": "Actual verification passed.", "execution_claims": [claim]}
    valid = normalize_turn(decode_text_turn(canonical_json({"tool": "done", "args": valid_args})), snapshot=restored)
    assert valid.control is not None and valid.control.kind == "done"
    session.last_done_args = {"execution_claims": [claim]}
    assert validate_execution_claims(session) is not None
    wrong = {**claim, "status": "success"}
    invalid = normalize_turn(decode_text_turn(canonical_json({"tool": "done", "args": {
        **valid_args, "execution_claims": [wrong]}})), snapshot=restored)
    assert invalid.protocol_error
    session.last_done_args = {"execution_claims": [wrong]}
    with pytest.raises(ValueError, match="disagrees"):
        validate_execution_claims(session)
