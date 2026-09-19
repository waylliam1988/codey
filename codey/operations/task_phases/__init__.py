"""Task run phase helpers, split by concern; no mode behavior lives here."""

from codey.operations.task_phases.dispatch import (
    connect_and_build_frame,
    dispatch_run_mode,
    project_completion_deps,
    record_route_trace,
    review_flow_deps,
)
from codey.operations.task_phases.ghost import claim_or_route_ghost_work, ghost_task_deps
from codey.operations.task_phases.hooks import build_hooks, record_provider_failure_event
from codey.operations.task_phases.lifecycle import (
    build_run_work,
    ensure_run_reserved_and_started,
    finish_run_operation,
    open_run_ledger,
    open_run_trace,
    start_run_operation,
)
from codey.operations.task_phases.settlement import (
    finish_mode_outcome,
    settle_cancelled_run,
    settle_error_run,
)

__all__ = [
    "build_hooks",
    "build_run_work",
    "claim_or_route_ghost_work",
    "connect_and_build_frame",
    "dispatch_run_mode",
    "ensure_run_reserved_and_started",
    "finish_mode_outcome",
    "finish_run_operation",
    "ghost_task_deps",
    "open_run_ledger",
    "open_run_trace",
    "project_completion_deps",
    "record_provider_failure_event",
    "record_route_trace",
    "review_flow_deps",
    "settle_cancelled_run",
    "settle_error_run",
    "start_run_operation",
]
