"""Public entry for the single operation execution driver."""
from __future__ import annotations

from codey.operations.kernel_contracts import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelResult,
    KernelRunRequest,
    KernelTransportDeps,
)
from codey.operations.kernel_driver import drive_kernel
from codey.operations.task_session import TaskSession

__all__ = ["KernelExecutionDeps", "KernelObservationDeps", "KernelResult", "KernelRunRequest",
           "KernelTransportDeps", "run_task_kernel"]


def run_task_kernel(session: TaskSession, *, request: KernelRunRequest) -> KernelResult:
    return drive_kernel(session, request)
