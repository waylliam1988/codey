"""Newly discovered verification hints must reach native continuations."""
import pytest

from codey.agents.request import AgentRequest
from codey.completion.verification_policy import discover_verification_candidates
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.workspace.revision import WorkspaceRevisionStore
from tests.support.kernel_runtime import run_agent


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_native_receipts_include_refreshed_coding_context_after_creation(tmp_path, scoped, enabled):
    messages = []
    observations = []
    replies = iter((
        ProviderToolCall("c1", "edit", {"path": "app.py", "content": "VALUE = 7\n"}),
        ProviderToolCall("c2", "edit", {"path": "test_app.py", "content":
            "import unittest\nfrom app import VALUE\nclass Tests(unittest.TestCase):\n"
            "    def test_value(self):\n        self.assertEqual(VALUE, 7)\n"}),
        ProviderToolCall("c3", "run", {"command": "python -m unittest test_app.py", "path": "."}),
        ProviderToolCall("c4", "done", {"summary": "Created and tested the files."}),
        ProviderToolCall("c5", "run", {"command": "python -m unittest discover", "path": "."}),
        ProviderToolCall("c6", "done", {"summary": "Created and tested the files."}),
    ))

    class Provider:
        native_tools = True
        request_scoped_tool_context = scoped

        def new_chat(self):
            pass

        def send_turn(self, text, tools, **kwargs):
            return AssistantTurn(tool_calls=(next(replies),))

        def send_tool_results(self, results, tools, **kwargs):
            messages.extend(results)
            observations.append(kwargs.get("tool_call_observations", ""))
            return AssistantTurn(tool_calls=(next(replies),))

        def acknowledge_tool_results(self, results, tools, **kwargs):
            return AssistantTurn()

    result = run_agent(AgentRequest(provider=Provider(), project=tmp_path,
        task="Create app.py and test_app.py, then run tests.", max_turns=8, provider_id="local",
        on_event=lambda _event: None, coding_context_enabled=enabled,
        workspace_revision_store=WorkspaceRevisionStore(tmp_path.parent / (tmp_path.name + "-state")),
        verification_candidate_loader=lambda: discover_verification_candidates(tmp_path)))
    assert result.stop_reason == "done"
    assert result.turns == 6
    continuation = "\n".join([*(message.user_content or "" for message in messages), *observations])
    receipts = "\n".join(message.content for message in messages)
    assert "Required verification for the current changes: python -m unittest discover (path: .)" in receipts
    assert ("Coding current local context:" in continuation) is enabled
    if enabled:
        assert "Suggested verification for current changes:" in continuation
        assert "python -m unittest discover" in continuation
    assert "Reply with exactly one JSON object" not in continuation
    assert all(message.call_id for message in messages)
