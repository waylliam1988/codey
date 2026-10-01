"""Cold start boundaries reject failures instead of inventing success."""

from types import SimpleNamespace

import pytest

from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy


def test_project_fresh_chat_failure_never_reuses_previous_dialogue():
    from codey.operations.project_adapter import _open_fresh_chat

    class Provider:
        def new_chat(self):
            raise RuntimeError("new chat failed")

    with pytest.raises(RuntimeError, match="new chat failed"):
        _open_fresh_chat(SimpleNamespace(fresh_chat=True, provider=Provider(),
                                        strict_fresh_chat=False, on_event=None))


def test_readonly_request_does_not_create_project_directory(tmp_path):
    from codey.agents.request import AgentRequest
    from codey.operations.project_adapter import run

    target = tmp_path / "missing"
    request = AgentRequest(provider=SimpleNamespace(send=lambda _: '{"tool":"done","args":{"summary":"done"}}'),
                           project=target, task="read",
                           fresh_chat=False, task_policy=TaskPolicy(grants=frozenset({"control"})))
    with pytest.raises((FileNotFoundError, RuntimeError)):
        run(request)
    assert not target.exists()


def test_explicit_source_executor_does_not_use_project_only_guard():
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.runtime.core.models import ToolCall

    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "web.read"})))
    calls = []
    results = execute_turn(session, [ToolCall("web_search", {"query":"q"})],
                           research_tools=SimpleNamespace(), snapshot=build_turn_snapshot(session),
                           executors={"web_search":lambda call: calls.append(call) or "searched"})
    assert len(calls) == 1
    assert results[0].model_text == "searched"


def test_false_verification_flag_cannot_be_overridden_by_exit_zero():
    from codey.operations.completion_gate import evaluate

    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write"})))
    session.edited_files = {"a.py": 1}
    fp = "sha256:" + "c" * 64
    session.set_workspace_state(1, fp)
    session.verifications = [{
        "revision": 1, "exit_code": 0, "passed": False,
        "command": "pytest", "cwd": ".",
        "workspace_revision": 1, "workspace_fingerprint": fp,
    }]
    assert evaluate(session, "done", context=None).complete is False


def test_synthesis_identity_does_not_collide_after_sanitizing_run_ids():
    from codey.operations.research_iteration import _stable_synthesis_id

    assert _stable_synthesis_id("run/a") != _stable_synthesis_id("run-a")
    assert _stable_synthesis_id("研究") != _stable_synthesis_id("run")


def test_synthesis_write_failure_does_not_report_created_note():
    from codey.operations.research_iteration import _persist_synthesis
    from codey.research.ledger import ResearchLedger

    class Store:
        def exists(self, note_id):
            return False

        def write_note(self, *args, **kwargs):
            raise OSError("disk full")

    tools = SimpleNamespace(store=Store(), changes=object(), created_ids=[], updated_ids=[],
                            ledger=ResearchLedger(), sources_read=set())
    assert not _persist_synthesis(tools, "question", "summary", session_id="s", project="",
                                  run_id="r", on_event=lambda _:None,
                                  policy=TaskPolicy(grants=frozenset({"knowledge.write"})))
    assert not tools.created_ids


def test_invalid_durable_identity_fails_before_chat_or_project_creation(tmp_path):
    from unittest import mock

    from codey.agents.request import AgentRequest
    from codey.operations.project_adapter import run

    provider = mock.Mock()
    project = tmp_path / "not-created"
    with pytest.raises(ValueError, match="provider_id"):
        run(AgentRequest(provider=provider, project=project, task="task",
                         runtime_mutations=object(), session_id="s", run_id="r"))
    provider.new_chat.assert_not_called()
    provider.send.assert_not_called()
    assert not project.exists()
