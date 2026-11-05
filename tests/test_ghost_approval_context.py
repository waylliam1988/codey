"""Approval resumes the original Ghost snapshot through the production entry."""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from codey.app import api, shell_service
from codey.app.context import AppContext
from codey.app.task_services import build_task_deps
from codey.operations.kernel_actions import _payload_strings
from codey.operations.task_entry import run_task_submission
from codey.providers import api_generation
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ProviderToolCall
from codey.task.model import ResolveShellApproval, StartTask, TaskSubmission
from tests.test_ghost_attempt_gate_lifecycle import PROTOCOLS, _reply

pytestmark = pytest.mark.usefixtures("scripted_local_api_connection")


@pytest.fixture(autouse=True)
def no_local_discovery(monkeypatch):
    def unexpected_discovery(**_kwargs):
        pytest.fail("Scripted approval tests must not discover a model on the host")

    monkeypatch.setattr("codey.providers.local_discovery.resolve_local_endpoint", unexpected_discovery)


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("restart", [False, True], ids=["warm", "restart"])
@pytest.mark.parametrize("control", ["enabled", "revoked", "expired"])
def test_approval_resume_binds_original_context(tmp_path, monkeypatch, protocol, restart, control):
    home = tmp_path / "state"
    state = AppContext(home, start_scheduler=False)
    provider = ApiProvider("http://fixture.invalid/v1", "fixture", api_protocol=protocol, native_tools=True)
    payloads = []
    replies = iter([
        _reply(protocol, call=ProviderToolCall("shell-1", "shell", {"command": "python --version", "path": "."})),
        _reply(protocol, call=ProviderToolCall("done-1", "done", {"summary": "Observed Python version."})),
    ])

    def generate(_endpoint, payload, *_args, **_kwargs):
        payloads.append(copy.deepcopy(payload))
        return next(replies)

    monkeypatch.setattr(api_generation, "generate", generate)
    if control == "expired":
        from codey.workspace.context_admission import sources_from_values

        expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        monkeypatch.setattr("codey.workspace.context_admission.sources_from_values", lambda **kwargs: tuple(
            replace(source, expires_at=expires) for source in sources_from_values(**kwargs)))
    monkeypatch.setattr(shell_service, "execute_shell_ticket", lambda *_: {
        "ok": True, "status": "exit", "output": "Python 3.11.9", "exit_code": 0})
    request = TaskSubmission.from_input(session_id="s", project=None, max_turns=4, provider_id="local",
        input=StartTask("Check the Python version."),
        intent="chat", run_id="r")
    events = []

    def run(submission):
        state.emit = events.append
        run_task_submission(build_task_deps(state, connect_provider=lambda _: provider), submission)
        return [event for event in events if event.get("type") == "task_done"][-1]

    try:
        with monkeypatch.context() as clock:
            clock.setattr("codey.ghost.continuity._common.now_iso_z",
                          lambda: (datetime.now(UTC) - timedelta(hours=1)).isoformat())
            state.ghost_continuity.sync_from_sources(user_focus_excerpt="Original unrelated focus.", session_id="s",
                run_id="previous", project="", mode="chat")
        first = run(request)
        assert first["stop_reason"] == "approval", first
        original_payload = json.dumps(payloads[0])
        assert "Original unrelated focus." in original_payload
        from codey.operations.ghost_context import ghost_continuity

        updated = ghost_continuity(state, session_id="s", project="").text
        assert updated and "Check the Python version." in updated
        assert "Original unrelated focus." in updated
        assert updated not in "\n".join(_payload_strings(payloads[0]))
        approval = state.durable.approval(next(iter(state.durable.task("r").state["approvals"])))
        status, _ = api.shell_approval_response(state, {"id": approval.approval_id, "approved": True},
                                               submit_task_after_slot_release=lambda *_: None)
        assert status == 202
        assert state.shell_approval_workflow.wait(5)
        if restart:
            provider.close()
            state.close()
            state = AppContext(home, start_scheduler=False)
            provider = ApiProvider("http://fixture.invalid/v1", "fixture", api_protocol=protocol, native_tools=True)
        if control == "revoked":
            monkeypatch.setattr(state.ghost_inbox, "control_enabled", lambda name, **_: name != "admit_hints")
        elif control == "expired":
            class FutureClock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return datetime.now(tz) + timedelta(hours=2)

            monkeypatch.setattr("codey.workspace.context_admission.datetime", FutureClock)
        resumed = run(replace(request, input=ResolveShellApproval(approval.approval_id)))
        blocked = control != "enabled"
        assert resumed["stop_reason"] == ("provider_failure" if blocked else "done"), resumed["summary"]
        assert len(payloads) == (1 if blocked else 2)
        if blocked:
            assert ("hints_disabled" if control == "revoked" else "expired") in resumed["summary"]
        else:
            assert "Original unrelated focus." in json.dumps(payloads[1])
            assert "Python 3.11.9" in json.dumps(payloads[1])
        assert all(effect.settlement is not None for effect in state.durable.effects("r"))
    finally:
        provider.close()
        state.close()
