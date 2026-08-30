from __future__ import annotations

import pytest

from codey.agents.handoff import ConversationContext
from codey.runtime.models import ToolCall
from codey.toolchain.runtime import ToolOutcome
from codey.runtime.events import RunEvent
from tests.manual import context_delta_ab


def test_followup_arms_change_only_context_delivery() -> None:
    conversation = ConversationContext()

    delta = context_delta_ab.followup_run_kwargs("delta", conversation)
    full = context_delta_ab.followup_run_kwargs("full", conversation)

    assert delta == {"fresh_chat": False, "conversation": conversation}
    assert full == {"fresh_chat": False, "conversation": None}


def test_unknown_followup_arm_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown arm"):
        context_delta_ab.followup_run_kwargs("other", ConversationContext())


def test_contract_delta_keeps_tool_contract_without_project_snapshot() -> None:
    task = "Explain the follow-up"

    prompt = context_delta_ab.followup_request("contract-delta", task)

    assert "careful local coding agent" in prompt
    assert "Project Map" in prompt
    assert "previously supplied" in prompt
    assert task in prompt
    assert "Initial listing:" not in prompt


def test_stage_metrics_counts_information_repeated_from_warmup() -> None:
    events = [
        RunEvent.tool_finished(
            1,
            ToolCall("read", {"path": "codey/agents/runner.py"}),
            ToolOutcome("content", True),
        ),
        RunEvent.tool_finished(
            2,
            ToolCall("search", {"path": ".", "query": "TaskFlow"}),
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
