"""Fixed production prompt surfaces remain unchanged for this runtime fix."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.operations import kernel_prompt
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.token_accounting import ContextBudget
from codey.runtime.core.models import ToolCall, ToolResult

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
    repair = kernel_prompt._repair_prompt("invalid_args", contract_text=contract, native=False)

    assert _sha256(initial) == "21cae739ba9e757b4c21e225689a8484c86f6d345c748268dfb14619fbae550b"
    assert _sha256(result) == "591c80828028a2103608e4d5a081bfd484d6d0fb755950c423cf8241b9876522"
    assert _sha256(repair) == "98200f1379c53fc3d9c08cbca01c32e88181fb6aec1d32f5a341b3c51b03739c"


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
    prompt = provider.prompts[0].replace(str(project), "<PROJECT>")
    assert _sha256(prompt) == "b8d235c4915fa95f73bac5a94bb6812fb2cf7adff33ae90bb806e64abc835976"
