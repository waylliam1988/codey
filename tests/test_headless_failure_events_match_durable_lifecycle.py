"""Real headless failure boundaries agree with durable state and never claim done."""

import json

import pytest

from codey.app.headless_runner import HeadlessAppContext, HeadlessRequest, run_headless
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.runtime.core import cancellation
from codey.runtime.core.operation_state import RuntimeOperationStore
from codey.runtime.effects.effect_records import RuntimeEffectStore
from codey.runtime.log.session_log import RuntimeSessionLog


@pytest.mark.parametrize("failure", ["connect", "cancel", "delivery"])
def test_failure_boundary_publishes_one_truthful_terminal_and_closes_provider(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("NATIVE_TOOLS", "1")
    monkeypatch.setattr(HeadlessAppContext, "provider_failover_order", lambda self: ("local",))
    project, state = tmp_path / "project", tmp_path / "state"
    rows = []

    class Provider:
        name = "Local"
        calls = 0
        closed = False

        def new_chat(self, timeout=None):
            pass

        def send(self, text, timeout=None):
            return json.dumps({"tool": "done", "args": {"summary": "unexpected"}})

        def send_turn(self, prompt, tools, timeout=None):
            self.calls += 1
            if failure == "cancel":
                cancellation.current_event().set()
            return AssistantTurn(tool_calls=(ProviderToolCall("c1", "edit", {"path": "made.py", "content": "x = 1\n"}),))

        def send_tool_results(self, messages, tools, timeout=None):
            self.receipts = messages
            if failure == "delivery":
                raise OSError("delivery unavailable")
            return AssistantTurn(text="cancel acknowledged")

        def close(self):
            self.closed = True

    provider = Provider()

    def connect(*args, **kwargs):
        if failure == "connect":
            raise OSError("connection unavailable")
        return provider

    result = run_headless(HeadlessRequest(project=project, task="Create made.py and verify it.", provider_id="local",
                                         max_turns=3, state_home=state, project_changes_required=True),
                          emit_jsonl=rows.append, connect_provider=connect)
    terminal = [row for row in rows if row["type"] == "task_done"]
    assert len(terminal) == 1, rows
    assert result.exit_code == 1
    assert result.stop_reason != "done"
    assert terminal[0]["stop_reason"] == result.stop_reason
    log = RuntimeSessionLog(state)
    operation = RuntimeOperationStore(log).load(result.session_id, result.run_id)
    assert operation.leaf == "terminal"
    assert operation.terminal.stop_reason == result.stop_reason
    if failure != "connect":
        assert provider.closed is True
    if failure in {"connect", "cancel"}:
        assert not (project / "made.py").exists()
    else:
        assert (project / "made.py").read_text() == "x = 1\n"
        assert provider.calls == 1
        assert provider.receipts[0]["tool_call_id"] == "c1"
        effects = RuntimeEffectStore(log).load_effects(result.session_id, result.run_id)
        assert len([effect for effect in effects if effect.intent.effect_category == "tool_call" and effect.is_settled]) == 1
