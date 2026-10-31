"""Reviewed production prompt baselines, including external-fact evidence guidance."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.operations import kernel_prompt
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.token_accounting import ContextBudget
from codey.runtime.core.models import ToolCall, ToolPlan, ToolResult

pytestmark = pytest.mark.usefixtures("scripted_local_api_connection")


class _PromptCaptureProvider:
    name = "Local"
    context_budget = ContextBudget(32768, 8192, 0, 12000)

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def bind_usage(self, connection_id, sink) -> None:
        return None

    def new_chat(self, timeout=None) -> None:
        return None

    def send(self, text: str, timeout=None) -> str:
        self.prompts.append(text)
        return json.dumps({"tool": "done", "args": {"summary": "answer"}})

    def close(self) -> None:
        return None


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_initial_result_and_repair_prompt_surfaces_have_frozen_hashes() -> None:
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read"})),
        task_kind="chat",
        task_text="Explain the fixed prompt contract.",
        project="E:/fixed-workspace",
    )
    contract = '{"tool":"done","args":{"summary":"<answer>"}}'
    initial = kernel_prompt.kernel_prompt_for_session(
        session,
        user_task="Explain the fixed prompt contract.",
        contract_text=contract,
        context_text="fixed context",
        native=False,
        tool_names=("done", "read_file"),
    )
    result = kernel_prompt._format_results(
        [ToolResult(call=ToolCall(name="read_file", args={"path": "inspect.py"}), model_text="answer", ok=True)],
        session,
    )
    repair = kernel_prompt._repair_prompt(ToolPlan(calls=[], control=None, protocol_error="invalid_args", protocol_tool_name="done"),
                                          snapshot=build_turn_snapshot(session), native=False)

    assert "obtain actual source data using the authorized tools" in initial
    assert "not evidence that the host has no network access" in initial
    assert _sha256(initial) == "c56b7f57796bc20e5ec1b896e682904de23ac951e714818c257977532bca381f"
    assert _sha256(result) == "591c80828028a2103608e4d5a081bfd484d6d0fb755950c423cf8241b9876522"
    # Reviewed: ordinary done repair preserves its task format; structured report
    # examples are supplied only by the frozen report task contract.
    assert _sha256(repair) == "0f0816d7864c4e82b38afcaee82d2dc06a0b84e0cce993a0bddcef00a6e32d43"


def test_real_task_entry_initial_prompt_has_a_frozen_hash(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    provider = _PromptCaptureProvider()
    result = run_headless(
        HeadlessRequest(
            project=project,
            task="Explain the fixed prompt contract.",
            provider_id="local",
            intent="chat",
            max_turns=2,
            state_home=tmp_path / "state",
        ),
        emit_jsonl=lambda _row: None,
        connect_provider=lambda *args, **kwargs: provider,
    )

    assert result.stop_reason == "done"
    assert len(provider.prompts) == 1
    prompt = (provider.prompts[0].replace(str(project), "<PROJECT>")
              .replace(json.dumps(str(project))[1:-1], "<PROJECT>"))
    # Reviewed with pinned knowledge references and structured report arguments.
    # The live prompt is hashed as one canonical surface so CodeMode/tool
    # contract changes cannot be silently accepted by a stale fixture.
    assert "ask_user" in prompt
    assert "Calls return {ok:true, value}" in prompt
    assert "Only frozen read-only tools" not in prompt
    assert _sha256(prompt) == "da4e3ab68be0fb259ab239c0338c8fcbfefcb224b316a416b6ff91bded5a5191"
