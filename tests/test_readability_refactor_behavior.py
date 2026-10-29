"""Observable contracts locked before the aesthetic extraction."""

from copy import deepcopy

import pytest

from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.runs.receipt import build_task_receipt, task_receipt_from_payload
from codey.runtime.core.operation_reducer import (
    ACTION_CONTINUE,
    ACTION_OBSERVE_BATCH_PROGRESS,
    ACTION_REPLAY_SAFE_TOOL_BATCH,
    next_runtime_action,
)
from codey.runtime.core.operation_state import DRIVER_WRITER, LEAF_TOOL_DELIVERY_PENDING
from codey.runtime.effects.replay_policy import ReplayClass
from codey.runtime.effects.tool_result_delivery import DeliveryBatchItem
from tests.support.kernel_protocol_test_helpers import normalize_turn
from tests.test_runtime_operation_reducer import _batch, _state, _tool_projection, _view


@pytest.mark.parametrize("settled", [False, True])
@pytest.mark.parametrize("attempted", [False, True])
def test_delivery_prefers_original_settlement_over_reexecution(settled, attempted):
    item = DeliveryBatchItem(0, "read", "eff-read", "safe", False)
    view = _view(
        _state(LEAF_TOOL_DELIVERY_PENDING, driver=DRIVER_WRITER, turn=1),
        effects=(_tool_projection("eff-read", settled=settled, replay_class=ReplayClass.SAFE),),
        batches=(_batch("batch", (item,), send_attempts=("send",) if attempted else ()),),
    )
    action = next_runtime_action(view)
    expected = (ACTION_OBSERVE_BATCH_PROGRESS if settled else
                ACTION_CONTINUE if attempted else ACTION_REPLAY_SAFE_TOOL_BATCH)
    assert action.kind == expected
    assert action.leaf == LEAF_TOOL_DELIVERY_PENDING
    assert action.delivery_batch_id == "batch"
    assert action.driver == DRIVER_WRITER
    assert action.effect_ids == (() if expected == ACTION_CONTINUE else ("eff-read",))


@pytest.mark.parametrize("section,key,value", [
    ("display", "summary", 0), ("display", "detail", False),
    ("work", "mode", []), ("work", "restore_available", 1),
    ("work", "changed_count", True), ("work", "changed_count", "1"),
    ("verification", "trust", []), ("verification", "checks_passed", 1),
    ("verification", "state", 1), ("verification", "stance", False),
    ("verification", "source", {}), ("verification", "proof_refs", "proof"),
    ("integrity", "status", []), ("integrity", "severity", False),
    ("integrity", "reason_codes", "reason"), ("integrity", "affected_paths", "file.py"),
    ("integrity", "refs", "ref"), ("integrity", "authorized_test_edit", "false"),
])
def test_receipt_shape_errors_cannot_be_repaired_into_a_valid_receipt(section, key, value):
    payload = deepcopy(build_task_receipt({"mode": "snapshot", "changed_count": 1}).to_dict())
    payload[section][key] = value
    assert task_receipt_from_payload(payload) is None


@pytest.mark.parametrize("checks_passed", [False, True])
def test_receipt_roundtrip_keeps_actual_facts_and_rejects_tampered_wording(checks_passed):
    receipt = build_task_receipt({"mode": "snapshot", "changed_count": 1}, checks_passed=checks_passed)
    payload = receipt.to_dict()
    assert task_receipt_from_payload(payload) == receipt
    payload["display"]["summary"] = "All tests passed"
    assert task_receipt_from_payload(payload) is None


@pytest.mark.parametrize("text", ["", "  \n"])
def test_empty_response_never_turns_into_done(text):
    plan = normalize_turn(text, policy=TaskPolicy(frozenset({"control"})))
    assert plan.protocol_error == "no JSON tool call found"
    assert not plan.calls
    assert plan.control is None


def test_native_text_reply_keeps_explicit_turn_tool_limits():
    reply = AssistantTurn(
        text="",
        tool_calls=(ProviderToolCall(id="call-1", name="read_file", arguments={"path": "a.py"}),),
    )
    plan = normalize_turn(reply, policy=TaskPolicy(frozenset({"control", "project.read"})),
                          snapshot_names=("done",))
    assert "not in the turn snapshot" in plan.protocol_error
    assert not plan.calls
