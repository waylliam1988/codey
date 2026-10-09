"""Closed rounds within one task can be replaced without losing the current intent."""
import copy

import pytest

from codey.providers import api_chat, api_responses


@pytest.mark.parametrize("codec", [api_chat, api_responses])
def test_closed_round_boundaries_preserve_latest_request_and_pending_call(codec):
    user = {"role": "user", "content": "Keep the public API unchanged"}
    if codec is api_chat:
        round_one = [{"role": "assistant", "tool_calls": [{"id": "a", "function": {"name": "read", "arguments": "{}"}}]},
                     {"role": "tool", "tool_call_id": "a", "content": "old result"}]
        pending = [{"role": "assistant", "tool_calls": [{"id": "b", "function": {"name": "read", "arguments": "{}"}}]}]
    else:
        round_one = [{"type": "reasoning", "encrypted_content": "opaque-a"},
                     {"type": "function_call", "call_id": "a", "name": "read", "arguments": "{}"},
                     {"type": "function_call_output", "call_id": "a", "output": "old result"}]
        pending = [{"type": "reasoning", "encrypted_content": "opaque-b"},
                   {"type": "function_call", "call_id": "b", "name": "read", "arguments": "{}"}]
    items = [user, *round_one, *pending]
    original = copy.deepcopy(items)
    spans = codec.closed_spans(items)
    assert (1, 1 + len(round_one)) in spans
    assert not any(end > 1 + len(round_one) for _, end in spans)
    assert items == original


@pytest.mark.parametrize("codec", [api_chat, api_responses])
def test_checkpoints_are_assistant_work_state_and_never_new_user_instructions(codec):
    item = codec.checkpoint_item("Goal: preserve API\nNext: read result exec-17")
    assert item["role"] == "assistant"
    assert "Current project state:" in str(item)
    assert all(word not in str(item) for word in ("compacted", "context window", "handoff"))


@pytest.mark.parametrize("codec", [api_chat, api_responses])
def test_a_new_user_question_does_not_make_the_latest_tool_result_summarizable(codec):
    from codey.providers.context_checkpoint import select_range
    if codec is api_chat:
        exchange = [{"role": "assistant", "tool_calls": [{"id": "a", "function": {"name": "run", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "a", "content": "Exit code: 1. Stored result: exec-17"}]
    else:
        exchange = [{"type": "function_call", "call_id": "a", "name": "run", "arguments": "{}"},
                    {"type": "function_call_output", "call_id": "a", "output": "Exit code: 1. Stored result: exec-17"}]
    items = [{"role": "user", "content": "old facts " * 3000}, *exchange,
             {"role": "assistant", "content": "Read the result"}, {"role": "user", "content": "What was the exit code?"}]
    selected = select_range(codec, items)
    assert selected is not None
    assert selected.end <= 1


def test_recent_tail_is_reserved_using_the_actual_request_counter():
    from codey.providers.api_provider import ApiProvider
    provider = ApiProvider("http://localhost:9/v1", "fixture", context_keep_recent_tokens=1000)
    provider._messages = [{"role": "user", "content": "old instructions " * 2000},
                          {"role": "assistant", "content": "recent investigation " * 60},
                          {"role": "assistant", "content": "recent next step " * 60},
                          {"role": "user", "content": "continue"}]
    seen = []
    provider.summarize_context = lambda source, _: (seen.append(source) or "Goal: keep old instructions")
    provider.maintain_context()
    provider.wait_for_maintenance(3)
    assert len(seen) == 1
    assert "recent investigation" not in str(seen[0])
    assert "recent next step" not in str(seen[0])
