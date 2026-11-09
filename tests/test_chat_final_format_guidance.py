"""Answer format follows task intent while tool envelopes retain their schema."""
from __future__ import annotations

import pytest

from codey.operations.kernel_prompt import _native_final_rule, kernel_prompt_for_session
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_guidance import task_guidance_for_policy
from codey.operations.task_session import TaskSession
from codey.policies.output_requirement import JSON_ANSWER_GUIDANCE
from codey.policies.task_policy import TaskPolicy
from tools.chat_answer_format_gate import baseline_format, format_ok


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("json_required", [False, True])
def test_default_language_and_explicit_json_requirements_have_separate_owners(native: bool, json_required: bool) -> None:
    policy = TaskPolicy(frozenset({"control"}), required_checks=("output_json",) if json_required else ())
    session = TaskSession(policy, task_kind="chat", task_text="What should I eat?")
    snapshot = build_turn_snapshot(session, native=native)
    prompt = kernel_prompt_for_session(session, tool_names=snapshot.tool_names, native=native,
        done_spec=next(spec for spec in snapshot.model_specs if spec.name == "done"),
        task_guidance=task_guidance_for_policy(policy, task_kind="chat"))
    assert "answer naturally in the user's language" in prompt
    assert "requested final format" in prompt
    assert (JSON_ANSWER_GUIDANCE in prompt) == json_required
    assert "Requested JSON is a bare document" not in prompt
    assert "including JSON if requested" not in prompt


def test_structured_research_completion_retains_its_submission_contract() -> None:
    text = _native_final_rule(structured_report=True)
    assert "report_sections.coverage" in text
    assert "Do not add summary" in text
    assert "answer naturally" not in text


def test_baseline_experiment_restores_only_reviewed_format_fragments_and_reverts() -> None:
    session = TaskSession(TaskPolicy(frozenset({"control"})), task_kind="chat")
    before = kernel_prompt_for_session(session, tool_names=("done",), native=True)
    from codey.operations import kernel_prompt

    with baseline_format(True):
        baseline = kernel_prompt.kernel_prompt_for_session(session, tool_names=("done",), native=True)
        assert "Requested JSON is a bare document" in baseline
        assert "answer naturally" not in baseline
    assert kernel_prompt.kernel_prompt_for_session(session, tool_names=("done",), native=True) == before


def test_live_format_checks_distinguish_requested_code_json_and_table() -> None:
    assert format_ok("json", '{"product":323}')
    assert not format_ok("json", '```json\n{"product":323}\n```')
    assert format_ok("table", "| 美食 | 主要原料 |\n|---|---|\n| 鱼丸 | 鱼肉 |\n| 肉燕 | 猪肉 |")
    assert not format_ok("table", "鱼丸使用鱼肉，肉燕使用猪肉。")
    assert format_ok("code", 'def greet(name: str) -> str:\n    return "你好" + name + "\\n欢迎"')
    assert not format_ok("code", 'def greet(name):\n    return "你好"')
    assert not format_ok("natural", '{"美食":["鱼丸","肉燕","锅边糊"]}')
