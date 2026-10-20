from __future__ import annotations

from types import SimpleNamespace

import pytest

from codey.ghost.admission import (
    GhostAdmissionError,
    GhostContextSource,
    admit_ghost_context,
    request_scope,
)
from codey.operations.task_loop import KernelRunRequest, KernelTransportDeps, run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy


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
                context_text="Task context: no memory",
                ghost_sources=(source,),
            ),
        ),
    )

    assert outcome.stop_reason == "provider_failure"
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
                context_text=source.text,
                ghost_sources=(source,),
            ),
        ),
    )

    assert outcome.stop_reason == "done"
    assert len(sent) == 1
    assert source.text in sent[0]
