from __future__ import annotations

import pytest

from codey.agents.handoff import ConversationContext
from codey.runtime.core.models import ToolCall
from codey.runtime.observe.events import RunEvent
from codey.toolchain.runtime import ToolOutcome
from tests.manual import context_delta_ab


def test_followup_arms_change_only_session_window_and_handoff() -> None:
    conversation = ConversationContext()

    continued = context_delta_ab.followup_run_kwargs("continued", conversation)
    fresh = context_delta_ab.followup_run_kwargs("fresh-handoff", conversation)

    assert continued == {"fresh_chat": False, "conversation": conversation, "handoff": ""}
    assert fresh["fresh_chat"] is True
    assert fresh["conversation"] is conversation
    assert isinstance(fresh["handoff"], str)


def test_unknown_followup_arm_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown arm"):
        context_delta_ab.followup_run_kwargs("other", ConversationContext())


def test_stage_metrics_counts_information_repeated_from_warmup() -> None:
    events = [
        RunEvent.tool_finished(
            1,
            ToolCall("read", {"path": "codey/agents/runner.py"}),
            ToolOutcome("content", True),
        ),
        RunEvent.tool_finished(
            2,
            ToolCall("search", {"path": ".", "query": "task_run"}),
            ToolOutcome("match", True),
        ),
    ]

    metrics = context_delta_ab._stage_metrics(
        events,
        {("read", "codey/agents/runner.py", "")},
    )

    assert metrics["information_calls"] == 2
    assert metrics["repeated_warmup_information_calls"] == 1
    assert metrics["tool_counts"] == {"read": 1, "search": 1}


def test_benchmark_mutations_are_hard_disabled() -> None:
    outcome = context_delta_ab._read_only_error()

    assert not outcome.ok
    assert not outcome.changed
    assert "read-only" in outcome.model_text


def test_default_output_stays_outside_repository() -> None:
    assert context_delta_ab.ROOT not in context_delta_ab.DEFAULT_OUTPUT.parents
