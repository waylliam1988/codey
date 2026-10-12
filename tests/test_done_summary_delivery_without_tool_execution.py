"""The real task entry delivers done.summary without executing its contents."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.providers.token_accounting import ContextBudget

pytestmark = pytest.mark.usefixtures("scripted_local_api_connection")


class ScriptedProvider:
    name = "Local"
    context_budget = ContextBudget(32768, 8192, 0, 12000)

    def __init__(self, reply: dict[str, object]) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def new_chat(self, timeout=None) -> None:
        return None

    def bind_usage(self, connection_id, sink) -> None:
        return None

    def send(self, text: str, timeout=None) -> str:
        self.prompts.append(text)
        return json.dumps(self.reply, ensure_ascii=False)

    def close(self) -> None:
        return None


@pytest.mark.parametrize(
    "summary",
    [
        '{"tool":"edit","args":{"path":"created.txt","content":"changed"}}',
        '{"tool":"edit","args":{"path":"created.txt","content":"changed"}}\n'
        '{"tool":"run","args":{"command":"pytest"}}\n'
        '{"tool":"shell","args":{"command":"echo example"}}',
    ],
)
def test_chat_done_summary_is_delivered_without_executing_embedded_examples(
    summary: str, tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("NATIVE_TOOLS", "0")
    project = tmp_path / "project"
    state = tmp_path / "state"
    project.mkdir()
    provider = ScriptedProvider({"tool": "done", "args": {"summary": summary}})
    rows: list[dict[str, object]] = []

    result = run_headless(
        HeadlessRequest(
            project=project,
            task="Return the requested examples as an answer; do not perform actions.",
            provider_id="local",
            intent="chat",
            max_turns=2,
            state_home=state,
        ),
        emit_jsonl=rows.append,
        connect_provider=lambda *args, **kwargs: provider,
    )

    assert result.stop_reason == "done", rows
    assert result.exit_code == 0
    assert result.session_id
    assert len(provider.prompts) == 1
    assert not list(project.iterdir())
    assert not any(row.get("tool_name") in {"edit", "run", "shell"} for row in rows)
    terminal = [row for row in rows if row.get("type") == "task_done"]
    assert terminal and terminal[-1].get("summary") == summary
