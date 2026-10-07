"""The kernel exchanges frozen definitions and results, never API wire messages."""
from dataclasses import fields
from types import SimpleNamespace

from codey.operations import kernel_protocol, kernel_transport, task_loop
from codey.policies.task_policy import TaskPolicy
from codey.providers import base


def test_turn_snapshot_has_no_chat_completions_tool_wrapper():
    session = SimpleNamespace(policy=TaskPolicy(grants=frozenset({"control"})), controller_denied=())
    snapshot = kernel_protocol.build_turn_snapshot(session, native=True)
    assert snapshot.frozen_specs
    assert "native_tools" not in {f.name for f in fields(snapshot)}


def test_native_result_delivery_contains_no_api_message_keys(monkeypatch):
    monkeypatch.setattr("codey.operations.kernel_prompt._result_context", lambda result, session: "ERROR: denied")
    result = SimpleNamespace(call=SimpleNamespace(call_id="call-exact"))
    delivered = kernel_transport._native_tool_results([result], object())
    assert delivered == [base.ProviderToolResult("call-exact", "ERROR: denied")]


def test_output_limit_is_a_typed_fact_for_every_native_provider():
    reply = base.AssistantTurn(text="unfinished", finish=base.TurnFinish.OUTPUT_LIMIT)
    action = task_loop._length_reply_action(reply, native=True, used=False, turns=1)
    assert isinstance(action, str)
    stopped = task_loop._length_reply_action(reply, native=True, used=True, turns=2)
    assert stopped.stop_reason == "provider_failure"


def test_raw_diagnostics_do_not_authorize_output_limit_continuation():
    reply = base.AssistantTurn(text="diagnostic", raw={"continuable_length": True})
    assert task_loop._length_reply_action(reply, native=True, used=False, turns=1) is None
