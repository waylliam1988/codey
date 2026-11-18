"""Approval resumes the original Ghost snapshot through the production entry."""
from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Self

import pytest

from codey.app import api, shell_service
from codey.app.context import AppContext
from codey.app.task_services import build_task_deps
from codey.durable.model import thaw_json
from codey.operations.kernel_actions import _payload_strings
from codey.operations.task_entry import run_task_submission
from codey.providers import api_generation
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ProviderToolCall
from codey.task.model import ResolveShellApproval, StartTask, TaskSubmission
from codey.types.json_value import JsonObject, decode_json_object
from codey.workspace.context_admission import GhostContextSource, sources_from_values

PROTOCOLS = ("openai-completions", "openai-responses")


def _reply(protocol: str, *, call: ProviderToolCall) -> JsonObject:
    arguments = json.dumps(call.arguments)
    if protocol == "openai-completions":
        return {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": call.id, "type": "function", "function": {"name": call.name,
                "arguments": arguments}}]}}]}
    return {"status": "completed", "output": [{"type": "function_call", "id": "item-1", "call_id": call.id,
        "name": call.name, "arguments": arguments}]}

pytestmark = pytest.mark.usefixtures("scripted_local_api_connection")


@pytest.fixture(autouse=True)
def no_local_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_discovery(**_kwargs: object) -> None:
        pytest.fail("Scripted approval tests must not discover a model on the host")

    monkeypatch.setattr("codey.providers.local_discovery.resolve_local_endpoint", unexpected_discovery)


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("restart", [False, True], ids=["warm", "restart"])
@pytest.mark.parametrize("control", ["enabled", "revoked", "expired"])
def test_approval_resume_binds_original_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protocol: str, restart: bool, control: str,
) -> None:
    home = tmp_path / "state"
    state = AppContext(home, start_scheduler=False)
    provider = ApiProvider("http://fixture.invalid/v1", "fixture", api_protocol=protocol, native_tools=True)
    payloads: list[JsonObject] = []
    replies = iter([
        _reply(protocol, call=ProviderToolCall("seed", "done", {"summary": "Prior request received."})),
        _reply(protocol, call=ProviderToolCall("shell-1", "shell", {"command": "python --version", "path": "."})),
        _reply(protocol, call=ProviderToolCall("done-1", "done", {"summary": "Observed Python version."})),
    ])

    def generate(_endpoint: str, payload: Mapping[str, object], *_args: object, **_kwargs: object) -> JsonObject:
        payloads.append(decode_json_object(copy.deepcopy(payload)))
        return decode_json_object(next(replies))

    monkeypatch.setattr(api_generation, "generate", generate)
    if control == "expired":
        expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        def expiring(*, directive: object = "", continuity: object = "", experiences: object = "",
                     scope: str, budgets: tuple[int, int, int] = (900, 900, 1800)) -> tuple[GhostContextSource, ...]:
            return tuple(replace(source, expires_at=expires) for source in sources_from_values(
                directive=directive, continuity=continuity, experiences=experiences, scope=scope,
                budgets=budgets))
        monkeypatch.setattr("codey.workspace.context_admission.sources_from_values", expiring)
    def execute(_context: shell_service.ShellExecutionContext,
                _ticket: shell_service.ShellExecutionTicket) -> shell_service.ShellResult:
        return {"ok": True, "status": "exit", "output": "Python 3.11.9", "exit_code": 0}
    monkeypatch.setattr(shell_service, "execute_shell_ticket", execute)
    request = TaskSubmission.from_input(session_id="s", project=None, max_turns=4, provider_id="local",
        input=StartTask("Check the Python version."),
        intent="chat", run_id="r")
    events: list[dict[str, object]] = []

    def run(submission: TaskSubmission) -> dict[str, object]:
        monkeypatch.setattr(state, "emit", events.append)
        run_task_submission(build_task_deps(state, connect_provider=lambda _: provider), submission)
        return [event for event in events if event.get("type") == "task_done"][-1]

    try:
        seed = TaskSubmission.from_input(session_id="s", project=None, max_turns=2, provider_id="local",
            input=StartTask("Original unrelated focus."), intent="chat", run_id="previous")
        assert run(seed)["stop_reason"] == "done"
        payloads.clear()
        provider = ApiProvider("http://fixture.invalid/v1", "fixture", api_protocol=protocol, native_tools=True)
        first = run(request)
        assert first["stop_reason"] == "approval", first
        original_payload = json.dumps(payloads[0])
        assert "Original unrelated focus." in original_payload
        from codey.operations.ghost_context import ghost_continuity

        updated = ghost_continuity(state, session_id="s", project="").text
        assert updated and "Check the Python version." not in updated
        assert "Original unrelated focus." in updated
        assert updated in "\n".join(_payload_strings(payloads[0]))
        approvals = decode_json_object(thaw_json(state.durable.task("r").state["approvals"]))
        approval = state.durable.approval(next(iter(approvals)))
        decision: dict[str, object] = {"id": approval.approval_id, "approved": True}
        response: object = api.shell_approval_response(state, decision,
                                                       submit_task_after_slot_release=lambda *_: None)
        assert isinstance(response, tuple)
        fields: tuple[object, ...] = response
        assert fields[0] == 202
        assert state.shell_approval_workflow.wait(5)
        if restart:
            provider.close()
            state.close()
            state = AppContext(home, start_scheduler=False)
        # The production entry closes its connection on suspension. Reconnect
        # while retaining the same application state for the warm case.
        provider = ApiProvider("http://fixture.invalid/v1", "fixture", api_protocol=protocol, native_tools=True)
        if control == "revoked":
            def enabled(name: str, **_scope: object) -> bool:
                return name != "admit_hints"
            monkeypatch.setattr(state.ghost_inbox, "control_enabled", enabled)
        elif control == "expired":
            class FutureClock(datetime):
                @classmethod
                def now(cls, tz: tzinfo | None = None) -> Self:
                    return cls.fromtimestamp(datetime.now(tz).timestamp() + 7200, tz)

            monkeypatch.setattr("codey.workspace.context_admission.datetime", FutureClock)
        resumed = run(replace(request, input=ResolveShellApproval(approval.approval_id)))
        blocked = control != "enabled"
        assert resumed["stop_reason"] == ("provider_failure" if blocked else "done"), resumed["summary"]
        assert len(payloads) == (1 if blocked else 2)
        if blocked:
            reason = str(resumed["summary"])
            assert (any(code in reason for code in ("hints_disabled", "source_snapshot_changed"))
                    if control == "revoked" else "expired" in reason)
        else:
            assert "Original unrelated focus." in json.dumps(payloads[1])
            assert "Python 3.11.9" in json.dumps(payloads[1])
        assert all(effect.settlement is not None for effect in state.durable.effects("r"))
    finally:
        provider.close()
        state.close()
