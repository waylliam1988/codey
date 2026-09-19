"""RunHooks assembly: event fan-out, shell approval, failure recording.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from codey.agents.shell_approval import ShellApprovalRequest
from codey.operations.context import RunHooks, RunWork
from codey.operations.project_completion_flow import (
    ProjectCompletionDeps,
    handle_project_tool_event,
)
from codey.policies.shell_risk import classify_shell_risk
from codey.providers import PROVIDER_LABELS
from codey.providers.diagnostics import ProviderFailure
from codey.runs.ledger import RunLedgerWriter
from codey.runtime.observe.events import (
    RunEvent,
    render_run_event,
    run_event_ui_payload,
)
from codey.runtime.observe.prompt_envelope import FailOpenPromptTrace
from codey.runtime.observe.terminalizer import nonnegative_event_count

logger = logging.getLogger(__name__)

def record_provider_failure_event(
    append_ledger_provider_failure: Callable[[str, ProviderFailure], None],
    trace_sink: Any,
    supervisor: Any,
    self_repair: Any,
    pid: str,
    failure: ProviderFailure,
) -> None:
    """Fan out one provider failure to ledger, trace, supervisor, self-repair."""
    append_ledger_provider_failure(pid, failure)
    trace_sink.call("record_provider_failure", pid, failure)
    if supervisor is None:
        return
    health = supervisor.record_failure(pid, failure)
    if self_repair is not None:
        try:
            self_repair.maybe_enqueue(pid, failure, health)
        except Exception:
            logger.exception("self-repair enqueue failed for provider %s", pid)


def build_hooks(
    deps: Any,
    state: Any,
    work: RunWork,
    *,
    session_id: str,
    run_id: str,
    project: str | None,
    max_turns: int,
    project_config_ignored: tuple[str, ...],
    review_log_lines: int,
    project_completion_deps: ProjectCompletionDeps,
    current_provider_id: Callable[[], str] | None = None,
) -> RunHooks:
    """Assemble RunHooks closures; behavior identical to the previous inline block."""
    logged_provider_failures: set[tuple[str, str, str, str]] = set()

    def append_ledger(action: Callable[[RunLedgerWriter], None]) -> None:
        if work.ledger is None:
            return
        try:
            action(work.ledger)
        except Exception:
            work.ledger = None

    def append_ledger_provider_failure(pid: str, failure: ProviderFailure) -> None:
        key = (
            str(pid),
            str(getattr(failure, "action", "")),
            str(getattr(failure, "kind", "")),
            str(getattr(failure, "message", "")),
        )
        if key in logged_provider_failures:
            return
        logged_provider_failures.add(key)
        append_ledger(lambda ledger: ledger.append_provider_failure(pid, failure))

    def update_checkpoint(action: Callable[[Any, Any], Any]) -> None:
        if deps.work_checkpoints is None or work.work_checkpoint is None:
            return
        with suppress(OSError, ValueError):
            work.work_checkpoint = action(deps.work_checkpoints, work.work_checkpoint)

    def on_event(event: RunEvent) -> None:
        work.turns_observed = max(work.turns_observed, nonnegative_event_count(event.turn))
        if work.record_agent_events_in_ledger:
            append_ledger(lambda ledger: ledger.append_run_event(event))
        payload = run_event_ui_payload(run_id, session_id, event)
        if payload is not None:
            state.emit(payload)
        if event.kind == "tool_start":
            return
        if project and _workspace_edit_event(event):
            work.advance_workspace_revision(
                deps.workspace_revisions,
                project,
                ignored_paths=project_config_ignored,
            )
        work.evidence.record(event)
        message = render_run_event(event)
        work.recent_events.append(message)
        if len(work.recent_events) > review_log_lines * 2:
            del work.recent_events[:review_log_lines]
        if project and event.kind == "tool" and event.call is not None and event.outcome is not None:
            handle_project_tool_event(
                project_completion_deps,
                event=event,
                project=project,
                work=work,
                run_id=run_id,
                update_checkpoint=update_checkpoint,
            )

    def on_shell_request(approval: ShellApprovalRequest) -> None:
        if not project:
            return
        cwd_rel = approval.cwd or "."
        risk = classify_shell_risk(approval.command)
        approval_id = "shell_" + uuid.uuid4().hex[:12]
        deferred_tool_calls = [item.to_payload() for item in approval.deferred_calls]
        provider_label = current_provider_id() if current_provider_id is not None else ""
        pending = {
            "id": approval_id,
            "session_id": session_id,
            "project": project,
            "cwd": cwd_rel or ".",
            "command": approval.command,
            "risk_label": risk.label,
            "risk_title": risk.title,
            "risk_detail": risk.detail,
            "post_approval_instructions": risk.post_approval_instructions,
            "max_turns": max_turns,
            "provider": provider_label,
            "continue_after": True,
            "run_id": run_id,
            "deferred_tool_count": len(approval.deferred_calls),
            "deferred_tool_calls": deferred_tool_calls,
        }
        pending["ui_event"] = {
            "type": "shell_request",
            "run_id": run_id,
            "session_id": session_id,
            "id": approval_id,
            "project": project,
            "cwd": pending["cwd"],
            "command": approval.command,
            "risk_label": risk.label,
            "risk_title": risk.title,
            "risk_detail": risk.detail,
            "deferred_tool_count": len(approval.deferred_calls),
            "deferred_tool_calls": deferred_tool_calls,
        }
        state.add_pending_shell_approval(approval_id, pending)
        state.emit(pending["ui_event"])

    supervisor = state.providers.supervisor
    self_repair = getattr(state, "self_repair", None)

    def record_provider_failure(pid: str, failure: ProviderFailure) -> None:
        record_provider_failure_event(
            append_ledger_provider_failure,
            FailOpenPromptTrace(work.trace),
            supervisor,
            self_repair,
            pid,
            failure,
        )

    def provider_failover_order() -> tuple[str, ...]:
        loader = getattr(state, "provider_failover_order", None)
        try:
            return tuple(loader()) if loader is not None else tuple(PROVIDER_LABELS)
        except Exception:
            return tuple(PROVIDER_LABELS)

    return RunHooks(
        on_event=on_event,
        on_shell_request=on_shell_request,
        update_checkpoint=update_checkpoint,
        record_provider_failure=record_provider_failure,
        append_ledger=append_ledger,
        provider_failover_order=provider_failover_order,
        supervisor=supervisor,
        trace=work.trace,
    )


def _workspace_edit_event(event: RunEvent) -> bool:
    return (
        event.kind == "tool"
        and event.call is not None
        and event.outcome is not None
        and event.call.name == "edit"
        and bool(event.outcome.ok)
        and bool(event.outcome.changed)
    )
