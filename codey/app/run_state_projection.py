"""Build run snapshots and publish execution-result projections."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from codey.app.approval_registry import ApprovalRegistry
from codey.app.event_payloads import SCHEMA_VERSION
from codey.app.run_registry import RunRegistry, RunSnapshot
from codey.app.task_lifecycle_projection import TaskLifecyclePayload, snapshot_tasks
from codey.durable.session import DurableSession
from codey.types.json_value import JsonObject, JsonValue

if TYPE_CHECKING:
    from codey.app.shell_approval_workflow import ShellApprovalWorkflow


class RunStateProjection:
    def __init__(
        self, *, registry: RunRegistry, approvals: ApprovalRegistry,
        workflow: ShellApprovalWorkflow, durable: DurableSession, lock: threading.Lock,
        research_runs: Callable[[], tuple[str, ...]], emit: Callable[[JsonObject], None],
    ) -> None:
        self._registry = registry
        self._approvals = approvals
        self._workflow = workflow
        self._durable = durable
        self._lock = lock
        self._research_runs = research_runs
        self._emit = emit

    def record_shell_result(self, event: JsonObject) -> None:
        if "failure" not in event:
            status = str(event.get("status") or "")
            failure_code = {
                "output_read_error": "output.unavailable",
                "drain_timeout": "output.unavailable",
                "timeout": "execution.uncertain",
                "wait_error": "execution.uncertain",
            }.get(status)
            if failure_code:
                event = {
                    **event,
                    "failure": {
                        "schema_version": SCHEMA_VERSION,
                        "code": failure_code,
                        "category": "execution" if failure_code.startswith("execution") else "output",
                        "scope": "tool", "phase": status, "effect": "unknown",
                        "incident_id": f"shell:{event.get('id') or 'unknown'}:{status}",
                        "session_id": str(event.get("session_id") or ""),
                        "run_id": str(event.get("run_id") or ""),
                        "execution_id": str(event.get("execution_id") or ""),
                    },
                }
        self._emit(self._registry.record_shell_result(event))

    def payload(self) -> JsonObject:
        with self._lock:
            research_runs = self._research_runs()
        payload = self._registry.payload(pending_event=self.pending_ui_event, research_restore_runs=research_runs)
        # ApprovalStatus contains only strings and integer revisions.
        payload["shell_approvals"] = cast(list[JsonValue], self._workflow.list_statuses())
        approval_work = self._workflow.busy_status()
        if not payload["busy"] and approval_work is not None:
            payload.update(busy=True, run_id=approval_work["source_run_id"],
                           session_id=approval_work["session_id"], run_status="running")
        payload["task_states"] = snapshot_tasks(self._durable, cast(TaskLifecyclePayload, payload))
        return payload

    def pending_ui_event(self, active: RunSnapshot | None) -> JsonObject | None:
        with self._lock:
            return cast(JsonObject | None, self._approvals.pending_ui_event(active))
