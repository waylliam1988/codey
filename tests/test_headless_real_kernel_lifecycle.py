"""Real headless entry and kernel, with scripted replies instead of a model."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.durable import DurableSession, SQLiteDurableStore
from codey.providers.token_accounting import ContextBudget
from codey.runtime.core.operation_state import RuntimeOperationStore
from tests.support.workspace_conversation import short_test_state_home

pytestmark = pytest.mark.usefixtures("scripted_local_api_connection")


class ScriptedProvider:
    name = "Local"
    context_budget = ContextBudget(32768, 8192, 0, 12000)

    def bind_usage(self, connection_id, sink):
        pass

    def __init__(self, replies):
        self.replies = iter(replies)
        self.prompts = []

    def new_chat(self):
        pass

    def send(self, text, timeout=None):
        self.prompts.append(text)
        return json.dumps(next(self.replies))

    def close(self):
        pass


def test_explicit_planning_enters_running_before_durable_provider_send(tmp_path, monkeypatch):
    monkeypatch.setenv("NATIVE_TOOLS", "0")
    project, state = tmp_path / "project", short_test_state_home()
    project.mkdir()
    (project / "pricing.py").write_text("READONLY_MARKER = 42\n", encoding="utf-8")
    provider = ScriptedProvider([
        {"tool": "read_file", "args": {"path": "pricing.py"}},
        {"tool": "done", "args": {"summary": "The file defines READONLY_MARKER as 42."}},
    ])
    rows = []
    result = run_headless(
        HeadlessRequest(project=project, task="Read pricing.py and explain without editing files.",
                        provider_id="local", intent="planning_readonly", max_turns=3, state_home=state),
        emit_jsonl=rows.append, connect_provider=lambda *a, **kw: provider,
    )
    assert result.stop_reason == "done", rows
    assert len(provider.prompts) == 2
    assert any(row.get("tool_name") == "read_file" and row.get("ok") is True for row in rows)
    durable = DurableSession(SQLiteDurableStore(state / "durable.sqlite3"))
    try:
        operation = RuntimeOperationStore(durable=durable).load(
            result.session_id, result.run_id
        )
        assert operation is not None
        assert operation.leaf == "terminal"
        assert durable.task(result.run_id).phase == "completed"
    finally:
        durable.close()
    assert not list((state / "runtime" / "sessions").glob("*.jsonl"))
    assert (project / "pricing.py").read_text() == "READONLY_MARKER = 42\n"


def test_headless_shell_denial_stops_without_fabricating_execution_receipts(tmp_path, monkeypatch):
    monkeypatch.setenv("NATIVE_TOOLS", "0")
    project, state = tmp_path / "project", short_test_state_home()
    project.mkdir()
    provider = ScriptedProvider([
        {"tool": "shell", "args": {"command": "mkdir -p unapproved", "path": "."}},
    ])
    rows = []
    # This lifecycle test owns one scripted provider.  Project startup also
    # has optional secondary advisor/audit hooks; isolate those hooks so the
    # test cannot probe real browser providers before the shell denial branch.
    with patch("codey.app.consensus_service.run_consensus", return_value=None), patch(
        "codey.app.consensus_service.run_project_audit", return_value=(),
    ):
        result = run_headless(
            HeadlessRequest(project=project, task="Create a file, asking for approval if needed.",
                            provider_id="local", intent="project", max_turns=3, state_home=state),
            emit_jsonl=rows.append, connect_provider=lambda *a, **kw: provider,
        )
    assert result.stop_reason == "stopped", [
        (row.get("stop_reason"), row.get("summary")) for row in rows if row.get("type") == "task_done"
    ]
    assert result.exit_code == 1
    assert any(row.get("type") == "shell_rejected" for row in rows)
    assert not (project / "unapproved").exists()
    durable = DurableSession(SQLiteDurableStore(state / "durable.sqlite3"))
    try:
        operation = RuntimeOperationStore(durable=durable).load(
            result.session_id, result.run_id
        )
        assert operation is not None
        # The stop occurred inside approval publication. Neither the original
        # shell call nor its provider boundary committed a settlement. A
        # scheduler cancellation cannot manufacture them to seal operation.
        assert operation.leaf == "tool_effect_pending"
        assert operation.terminal is None
        effects = durable.effects(result.run_id)
        assert len(effects) == 2 and all(effect.settlement is None for effect in effects)
        assert any(artifact.metadata.get("kind") == "provider_response"
                   for artifact in durable.artifacts_for_task(result.run_id))
        assert len(provider.prompts) == 1
        # Cancellation is a first-class durable terminal state; the
        # user-facing stop_reason remains ``stopped``.
        assert durable.task(result.run_id).phase == "cancelled"
    finally:
        durable.close()
