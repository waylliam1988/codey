"""Application services used to assemble and coordinate a task submission."""
from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from codey.operations.task_state import TaskState, TaskSubmissionStores

if TYPE_CHECKING:
    from codey.app.approval_registry import ApprovalRegistry
    from codey.app.lifecycle import AppLifecycle
    from codey.app.provider_registry import ProviderRegistry
    from codey.app.run_registry import RunRegistry, RunSnapshot
    from codey.app.shell_ticket import ShellExecutionTicket
    from codey.app.task_runtime import TaskRuntimeCoordinator
    from codey.durable.scheduler import DurableTaskScheduler


class AppTaskState(TaskState, Protocol):
    @property
    def run_registry(self) -> RunRegistry: ...
    @property
    def providers(self) -> ProviderRegistry: ...
    task_runtime: TaskRuntimeCoordinator
    lifecycle: AppLifecycle
    approvals: ApprovalRegistry

    def reserve_run(
        self, *, session_id: str, project: str | None, task: str, provider_id: str,
        run_id: str = "", abort_if_stopped: bool = False,
    ) -> RunSnapshot | None: ...
    def current_run(self) -> RunSnapshot | None: ...
    def active_run_for(self, *, session_id: str = "", project: str | None = None) -> RunSnapshot | None: ...

    def claim_shell_ticket(
        self, approval_id: str, *, timeout: int, output_limit: int,
    ) -> tuple[dict[str, object] | None, ShellExecutionTicket | None]: ...


class TaskSubmissionState(AppTaskState, Protocol):
    durable_scheduler: DurableTaskScheduler

    @property
    def task_submission_stores(self) -> TaskSubmissionStores: ...
