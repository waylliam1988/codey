"""Rejected worker starts must not retain approval execution ownership."""

from __future__ import annotations

import threading
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Protocol

import pytest

from codey.app.context import AppContext
from codey.app.shell_service import ShellExecutionContext, ShellExecutionTicket, ShellResult
from tests.support.shell_approval import seed_requested_approval


class ThreadStarter(Protocol):
    def start(self, worker: threading.Thread, /) -> None: ...


THREAD_STARTER: ThreadStarter = threading.Thread


@pytest.mark.parametrize("entry", ("accept", "recovery"))
def test_failed_worker_start_releases_admission_for_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str,
) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        requested = seed_requested_approval(state, continue_after=False)
        original_start = THREAD_STARTER.start
        failures: list[str] = []
        executions: list[str] = []

        def fail_once(self: threading.Thread) -> None:
            if self.name == "approval-approval" and not failures:
                failures.append(self.name)
                raise RuntimeError("worker start rejected")
            original_start(self)

        def execute(ctx: ShellExecutionContext, ticket: ShellExecutionTicket) -> ShellResult:
            executions.append(ticket.execution_id)
            return {"ok": True, "status": "exit", "output": "done", "exit_code": 0}

        def submit() -> None:
            raise AssertionError("compatibility callback must not own durable execution")

        monkeypatch.setattr(THREAD_STARTER, "start", fail_once)
        monkeypatch.setattr("codey.app.shell_service.execute_shell_ticket", execute)
        workflow = state.shell_approval_workflow
        with pytest.raises(RuntimeError, match="worker start rejected"):
            if entry == "accept":
                workflow.accept("approval", True, submit)
            else:
                assert entry == "recovery"
                state.record_shell_approval_fact(replace(requested, kind="approved"))
                workflow.start(submit)
        assert failures == ["approval-approval"]
        assert state.durable.approval("approval").status == "approved"
        assert workflow.quiescent()
        assert workflow.wait(0)
        assert executions == []
        workflow.start(submit)
        assert workflow.wait(3)
        assert len(executions) == 1
        assert state.durable.approval("approval").status == "execution_settled"
        workflow.start(submit)
        assert workflow.wait(3)
        assert len(executions) == 1


def test_an_admitted_worker_keeps_single_execution_ownership(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        requested = seed_requested_approval(state, continue_after=False)
        state.record_shell_approval_fact(replace(requested, kind="approved"))
        entered = threading.Event()
        release = threading.Event()
        executions: list[str] = []

        def execute(ctx: ShellExecutionContext, ticket: ShellExecutionTicket) -> ShellResult:
            executions.append(ticket.execution_id)
            entered.set()
            assert release.wait(3)
            return {"ok": True, "status": "exit", "output": "done", "exit_code": 0}

        def submit() -> None:
            raise AssertionError("compatibility callback must not own durable execution")

        monkeypatch.setattr("codey.app.shell_service.execute_shell_ticket", execute)
        workflow = state.shell_approval_workflow
        try:
            workflow.start(submit)
            assert entered.wait(3)
            workflow.start(submit)
            assert not workflow.quiescent()
            assert not workflow.wait(0)
            assert len(executions) == 1
        finally:
            release.set()
            assert workflow.wait(3)
        assert state.durable.approval("approval").status == "execution_settled"
        assert len(executions) == 1
