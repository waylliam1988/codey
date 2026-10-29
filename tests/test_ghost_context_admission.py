from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from codey.agents.request import AgentRequest
from codey.operations.project_adapter import _project_context, _project_ghost_sources
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.workspace.context_admission import (
    GhostAdmissionError,
    GhostContextSource,
    admit_ghost_context,
    request_scope,
)
from tests.support.kernel_runtime import KernelExecutionDeps, KernelRunRequest, KernelTransportDeps, run_task_kernel


def _source(text: str = "Prefer Chinese answers.", **kwargs) -> GhostContextSource:
    return GhostContextSource(
        key=kwargs.pop("key", "ghost_directive"),
        text=text,
        budget=kwargs.pop("budget", 900),
        scope=kwargs.pop("scope", request_scope("project-a", "session-a")),
    )


def test_admission_binds_source_digest_budget_scope_and_epoch_without_raw_text() -> None:
    source = _source()
    result = admit_ghost_context(
        f"Task context:\n{source.text}",
        (source,),
        request_scope=source.scope,
        require_all=True,
    )

    assert result.rejected == ()
    assert result.epoch.epoch_id.startswith("ctx_epoch:")
    payload = result.epoch.to_payload()
    admission = payload["admissions"][0]
    assert admission["source_ref"] == "context_source:ghost_directive"
    assert admission["budget"] == 900
    assert admission["scope"] == source.scope
    assert source.text not in repr(payload)


@pytest.mark.parametrize(
    ("prompt", "source", "reason"),
    [
        ("Task context: no memory", _source(), "not_in_provider_payload"),
        ("Task context: Prefer Chinese answers.", _source(budget=4), "over_budget"),
        (
            "Task context: Prefer Chinese answers.",
            _source(scope="project-other"),
            "scope_mismatch",
        ),
    ],
)
def test_required_source_rejection_fails_closed(prompt: str, source: GhostContextSource, reason: str) -> None:
    with pytest.raises(GhostAdmissionError) as caught:
        admit_ghost_context(prompt, (source,), request_scope=request_scope("project-a"), require_all=True)
    assert caught.value.rejections[0].reason == reason


def test_expired_context_source_is_rejected_with_explicit_as_of() -> None:
    source = GhostContextSource(
        "ghost_directive", "Prefer Chinese answers.", 900, request_scope("project-a"),
        expires_at="2026-10-20T00:00:00Z",
    )
    result = admit_ghost_context(
        source.text, (source,), request_scope=source.scope,
        as_of="2026-10-21T00:00:00Z",
    )
    assert result.accepted == ()
    assert result.rejected[0].reason == "expired"


def test_invalid_context_expiry_fails_closed() -> None:
    source = GhostContextSource(
        "ghost_directive", "Prefer Chinese answers.", 900, request_scope("project-a"),
        expires_at="not-a-timestamp",
    )
    with pytest.raises(GhostAdmissionError) as caught:
        admit_ghost_context(
            source.text, (source,), request_scope=source.scope,
            require_all=True, as_of="2026-10-21T00:00:00Z",
        )
    assert caught.value.rejections[0].reason == "invalid_expiry"


def test_real_kernel_send_is_blocked_before_provider_when_ghost_is_not_admitted() -> None:
    sent: list[str] = []

    def send(prompt: str) -> str:
        sent.append(prompt)
        return '{"tool":"done","args":{"summary":"unexpected"}}'

    source = _source()
    outcome = run_task_kernel(
        TaskSession(policy=TaskPolicy(grants=frozenset({"control"})), max_turns=1),
        request=KernelRunRequest(
            transport=KernelTransportDeps(
                provider=SimpleNamespace(send=send),
                run_id="ghost-admission-block",
                context_scope=request_scope("project-a", "session-a"),
                context_text="Task context: no memory",
                ghost_sources=(source,),
            ),
            execution=KernelExecutionDeps(project_path="project-a", session_id="session-a"),
        ),
    )

    assert outcome.stop_reason == "controller_failure"
    assert sent == []


def test_real_kernel_send_captures_admitted_ghost_bytes() -> None:
    sent: list[str] = []

    def send(prompt: str) -> str:
        sent.append(prompt)
        return '{"tool":"done","args":{"summary":"done"}}'

    source = _source()
    outcome = run_task_kernel(
        TaskSession(policy=TaskPolicy(grants=frozenset({"control"})), max_turns=1),
        request=KernelRunRequest(
            transport=KernelTransportDeps(
                provider=SimpleNamespace(send=send),
                run_id="ghost-admission-capture",
                context_scope=request_scope("project-a", "session-a"),
                context_text=source.text,
                ghost_sources=(source,),
            ),
            execution=KernelExecutionDeps(project_path="project-a", session_id="session-a"),
        ),
    )

    assert outcome.stop_reason == "done"
    assert len(sent) == 1
    assert source.text in sent[0]


def test_real_kernel_send_respects_admit_hints_control_before_provider() -> None:
    sent: list[str] = []

    def send(prompt: str) -> str:
        sent.append(prompt)
        return '{"tool":"done","args":{"summary":"unexpected"}}'

    source = _source()
    outcome = run_task_kernel(
        TaskSession(policy=TaskPolicy(grants=frozenset({"control"})), max_turns=1),
        request=KernelRunRequest(
            transport=KernelTransportDeps(
                provider=SimpleNamespace(send=send),
                run_id="ghost-admission-disabled",
                context_scope=request_scope("project-a", "session-a"),
                context_text=source.text,
                ghost_sources=(source,),
                admit_hints=False,
            ),
            execution=KernelExecutionDeps(project_path="project-a", session_id="session-a"),
        ),
    )

    assert outcome.stop_reason == "controller_failure"
    assert sent == []


def test_project_adapter_drops_ghost_text_when_admit_hints_is_disabled(tmp_path: Path) -> None:
    request = AgentRequest(
        provider=SimpleNamespace(),
        project=tmp_path,
        task="inspect project",
        ghost_directive="secret directive",
        ghost_continuity="secret continuity",
        ghost_experiences="secret history",
        admit_hints=False,
    )
    assert _project_ghost_sources(request) == ()
    context = _project_context(request)
    assert "secret directive" not in context
    assert "secret continuity" not in context
    assert "secret history" not in context
