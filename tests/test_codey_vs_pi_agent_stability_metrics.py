"""Completion, verification and usage truth in the real-agent comparison."""

from tests.manual.codey_vs_pi_agent_stability_ab import _metrics


def test_external_oracle_cannot_substitute_for_required_agent_verification():
    metrics = _metrics([{"type": "task_done", "stop_reason": "done"}], [],
                       {"passed": True, "agent_verification_fresh": False}, 0)
    assert metrics["task_success"] is False
    assert metrics["false_completion"] is True


def test_pi_settled_event_does_not_hide_an_assistant_error():
    rows = [{"type": "message_end", "message": {"role": "assistant", "stopReason": "error"}},
            {"type": "agent_settled"}]
    metrics = _metrics(rows, [], {"passed": True, "agent_verification_fresh": True}, 0)
    assert metrics["task_success"] is False


def test_pi_normal_final_response_is_checked_against_external_facts():
    rows = [{"type": "message_end", "message": {"role": "assistant", "stopReason": "stop",
             "content": [{"type": "text", "text": "Fixed and verified."}]}}, {"type": "agent_settled"}]
    metrics = _metrics(rows, [], {"passed": False, "agent_verification_fresh": False}, 0)
    assert metrics["false_completion"] is True


def test_zero_usage_is_known_zero_and_missing_exchange_makes_total_incomplete():
    known = {"path": "/v1/chat/completions", "response": {"usage": {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}}
    metrics = _metrics([], [known], {"passed": False}, 0)
    assert metrics["token_usage"] == 0
    assert metrics["usage_complete"] is True
    metrics = _metrics([], [known, {"path": "/v1/chat/completions", "response": {}}], {"passed": False}, 0)
    assert metrics["token_usage"] is None
    assert metrics["known_token_usage"] == 0
    assert metrics["usage_complete"] is False


def test_successful_noop_writes_are_not_duplicate_mutations():
    rows = []
    for call_id in ("a", "b"):
        rows.extend([{"type": "tool_started", "tool": "write", "tool_id": call_id,
                      "args": {"path": "app.py", "content": "same"}},
                     {"type": "tool", "tool_id": call_id, "ok": True, "changed": False}])
    assert _metrics(rows, [], {"passed": False}, 0)["mutation_calls"] == 0


def test_pi_explicit_blocked_response_is_not_false_completion():
    rows = [{"type": "message_end", "message": {"role": "assistant", "stopReason": "stop",
             "content": [{"type": "text", "text": '{"status":"blocked","reason":"changes forbidden"}'}]}},
            {"type": "agent_settled"}]
    assert _metrics(rows, [], {"passed": False}, 0)["false_completion"] is False


def test_rereading_a_changed_file_is_not_unproductive_repetition():
    rows = []
    for call_id, revision in (("a", "before"), ("b", "after")):
        rows.extend([{"type": "tool_started", "tool": "read", "tool_id": call_id,
                      "args": {"path": "app.py"}, "observed_workspace": revision},
                     {"type": "tool", "tool_id": call_id, "ok": True, "observed_result": "text"}])
    result = _metrics(rows, [], {"passed": False}, 0)
    assert result["repeated_tool_calls"] == 1
    assert result["unproductive_repeated_calls"] == 0


def test_codey_explicit_blocked_finish_is_scored_like_pi_blocked_finish():
    row = {"type": "task_done", "stop_reason": "done", "summary":
           '{"status":"blocked","reason":"changes forbidden"}'}
    result = _metrics([row], [], {"passed": False}, 0)
    assert result["blocked"] is True
    assert result["false_completion"] is False


def test_bounded_events_without_arguments_cannot_prove_a_repeated_call():
    rows = []
    for call_id in ("a", "b"):
        rows.extend([{"type": "tool_started", "tool": "read_tool_result", "tool_id": call_id},
                     {"type": "tool", "tool_id": call_id, "ok": True, "changed": False}])
    result = _metrics(rows, [], {"passed": False}, 0)
    assert result["repeated_tool_calls"] == 0
    assert result["unknown_argument_calls"] == 2


def test_recovered_middle_output_requires_a_read_instead_of_the_producer_or_test_source():
    from tests.manual.agent_stability_measurements import stored_output_was_read
    def exchange(name, args):
        return {"request": {"messages": [
            {"role": "assistant", "tool_calls": [{"id": "call", "function": {
                "name": name, "arguments": __import__("json").dumps(args)}}]},
            {"role": "tool", "tool_call_id": "call", "content": "REQUIRED_VALUE=river"}]}}
    assert not stored_output_was_read([exchange("bash", {"command": "python -m unittest"})], "REQUIRED_VALUE=river")
    assert not stored_output_was_read([exchange("read", {"path": "test_app.py"})], "REQUIRED_VALUE=river")
    assert stored_output_was_read([exchange("read_tool_result", {"offset": 210000})], "REQUIRED_VALUE=river")


def test_pi_successful_abort_acknowledgement_with_settled_agent_is_a_stop():
    # Native Pi reports error, rather than aborted, when cancellation happens
    # during a tool. The successful RPC acknowledgement confirms the request.
    rows = [{"type": "agent_start"},
            {"type": "message_end", "message": {"role": "assistant", "stopReason": "error"}},
            {"type": "agent_settled"},
            {"type": "response", "command": "abort", "success": True}]
    assert _metrics(rows, [], {"passed": False}, 0)["stopped"] is True
    assert _metrics(rows[:-2], [], {"passed": False}, 0)["stopped"] is False
    assert _metrics([*rows[:-1], {**rows[-1], "success": False}], [], {"passed": False}, 0)["stopped"] is False


def test_old_pi_abort_acknowledgement_does_not_mark_a_resumed_task_stopped():
    rows = [{"type": "agent_start"}, {"type": "agent_settled"},
            {"type": "response", "command": "abort", "success": True},
            {"type": "agent_start"},
            {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}},
            {"type": "agent_settled"}]
    result = _metrics(rows, [], {"passed": True, "agent_verification_fresh": True}, 0)
    assert result["stopped"] is False
    assert result["task_success"] is True
