"""RunHooks assembly: event fan-out, shell approval, failure recording.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from codey.agents.shell_approval import (
    ShellApprovalRequest,
    build_shell_approval_pending,
    shell_command_payload,
    shell_command_text,
)
from codey.operations.context import RunHooks, RunWork
from codey.operations.project_completion_context import (
    ProjectCompletionDeps,
    handle_project_tool_event,
)
from codey.operations.task_state import TaskState
from codey.policies.shell_risk import classify_shell_risk
from codey.providers.diagnostics import ProviderFailure
from codey.providers.supervisor import HealthStoreError
from codey.runs.ledger import LedgerWriteFailed, RunLedgerWriter
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
    try:
        health = supervisor.record_failure(pid, failure)
    except HealthStoreError:
        # The failure is already in the ledger; without durable health there
        # is nothing honest to enqueue for repair, so log and continue with
        # provider cleanup/failover instead of crashing the run.
        logger.exception("provider health record failed for %s", pid)
        return
    if self_repair is not None:
        try:
            self_repair.maybe_enqueue(pid, failure, health)
        except Exception:
            logger.exception("self-repair enqueue failed for provider %s", pid)


def record_provider_success_event(supervisor: Any, pid: str) -> None:
    """Record one provider success; a storage fault is logged, not fatal.

    The attempt genuinely succeeded, so the run continues with its result;
    later events rebuild the health picture once the store is writable.
    """
    if supervisor is None:
        return
    try:
        supervisor.record_success(pid)
    except HealthStoreError:
        logger.exception("provider health success record failed for %s", pid)


@dataclass(kw_only=True)
class _RunHookCallbacks:
    """Own callback state for one run; all callbacks share this lifetime."""

    deps: Any
    state: TaskState
    work: RunWork
    session_id: str
    run_id: str
    project: str | None
    max_turns: int
    project_config_ignored: tuple[str, ...]
    review_log_lines: int
    project_completion_deps: ProjectCompletionDeps
    current_provider_id: Callable[[], str] | None
    supervisor: Any
    self_repair: Any
    logged_provider_failures: set[tuple[str, str, str, str]] = field(default_factory=set)

    def append_ledger(self, action: Callable[[RunLedgerWriter], None]) -> None:
        if self.work.ledger is None:
            return
        try:
            action(self.work.ledger)
        except (LedgerWriteFailed, OSError, ValueError, TimeoutError) as exc:
            # Expected storage faults mark this run's ledger unavailable;
            # the task itself still completes. One bounded diagnostic, no
            # new persisted file.
            logger.warning(
                "run ledger unavailable: %s",
                str(exc)[:120],
            )
            self.work.ledger = None

    def append_ledger_provider_failure(self, pid: str, failure: ProviderFailure) -> None:
        key = (
            str(pid),
            str(getattr(failure, "action", "")),
            str(getattr(failure, "kind", "")),
            str(getattr(failure, "message", "")),
        )
        if key in self.logged_provider_failures:
            return
        self.logged_provider_failures.add(key)
        self.append_ledger(lambda ledger: ledger.append_provider_failure(pid, failure))

    def update_checkpoint(self, action: Callable[[Any, Any], Any]) -> None:
        if self.deps.work_checkpoints is None or self.work.work_checkpoint is None:
            return
        with suppress(OSError, ValueError):
            self.work.work_checkpoint = action(self.deps.work_checkpoints, self.work.work_checkpoint)

    def on_event(self, event: RunEvent) -> None:
        self.work.turns_observed = max(self.work.turns_observed, nonnegative_event_count(event.turn))
        if self.work.record_agent_events_in_ledger:
            self.append_ledger(lambda ledger: ledger.append_run_event(event))
        payload = run_event_ui_payload(self.run_id, self.session_id, event)
        if payload is not None:
            self.state.emit(payload)
        if event.kind == "tool_start":
            return
        if self.project and _workspace_edit_event(event) and not _adopt_kernel_workspace_state(self.work, event):
            # Kernel edits already bumped exactly once in
            # sync_workspace_state_after_edit and carry the authoritative
            # proof in the event side-channel. Adopt it without a second
            # bump/scan. Non-kernel edits carry no state and still bump.
            self.work.advance_workspace_revision(
                self.deps.workspace_revisions,
                self.project,
                ignored_paths=self.project_config_ignored,
            )
        self.work.evidence.record(event)
        message = render_run_event(event)
        self.work.recent_events.append(message)
        if len(self.work.recent_events) > self.review_log_lines * 2:
            del self.work.recent_events[:self.review_log_lines]
        if self.project and event.kind == "tool" and event.call is not None and event.outcome is not None:
            handle_project_tool_event(
                self.project_completion_deps,
                event=event,
                project=self.project,
                work=self.work,
                run_id=self.run_id,
                update_checkpoint=self.update_checkpoint,
            )

    def on_shell_request(self, approval: ShellApprovalRequest) -> None:
        if not self.project:
            return
        command = shell_command_text(approval.command)
        command_fields = shell_command_payload(command)
        risk = classify_shell_risk(command)
        approval_id = "shell_" + uuid.uuid4().hex[:12]
        provider_label = self.current_provider_id() if self.current_provider_id is not None else ""
        # pending 形状唯一归属 build_shell_approval_pending：原生调用身份
        #（call id、provider 会话、turn/tool_index）完整持久化。
        pending = build_shell_approval_pending(
            approval=approval,
            approval_id=approval_id,
            session_id=self.session_id,
            run_id=self.run_id,
            project=self.project,
            max_turns=self.max_turns,
            provider_label=provider_label,
            command_fields=dict(command_fields),
            risk_label=risk.label,
            risk_title=risk.title,
            risk_detail=risk.detail,
            post_approval_instructions=risk.post_approval_instructions,
        )
        self.state.add_pending_shell_approval(approval_id, pending)
        event = pending.get("ui_event")
        if isinstance(event, dict):
            self.state.emit(event)

    def record_provider_failure(self, pid: str, failure: ProviderFailure) -> None:
        record_provider_failure_event(
            self.append_ledger_provider_failure,
            FailOpenPromptTrace(self.work.trace),
            self.supervisor,
            self.self_repair,
            pid,
            failure,
        )

    def provider_failover_order(self) -> tuple[str, ...]:
        return self.state.provider_failover_order()


def build_hooks(
    deps: Any,
    state: TaskState,
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
    """Bind one run's event, approval, persistence and provider callbacks."""
    callbacks = _RunHookCallbacks(
        deps=deps,
        state=state,
        work=work,
        session_id=session_id,
        run_id=run_id,
        project=project,
        max_turns=max_turns,
        project_config_ignored=project_config_ignored,
        review_log_lines=review_log_lines,
        project_completion_deps=project_completion_deps,
        current_provider_id=current_provider_id,
        supervisor=state.providers.supervisor,
        self_repair=state.self_repair,
    )
    return RunHooks(
        on_event=callbacks.on_event,
        on_shell_request=callbacks.on_shell_request,
        update_checkpoint=callbacks.update_checkpoint,
        record_provider_failure=callbacks.record_provider_failure,
        append_ledger=callbacks.append_ledger,
        provider_failover_order=callbacks.provider_failover_order,
        supervisor=callbacks.supervisor,
        trace=work.trace,
    )


def _workspace_edit_event(event: RunEvent) -> bool:
    outcome = getattr(event, "outcome", None)
    return (
        event.kind == "tool"
        and event.call is not None
        and outcome is not None
        and event.call.name == "edit"
        and type(getattr(outcome, "ok", None)) is bool
        and outcome.ok is True
        and type(getattr(outcome, "changed", None)) is bool
        and outcome.changed is True
    )


def _adopt_kernel_workspace_state(work: RunWork, event: RunEvent) -> bool:
    """Adopt kernel-owned revision without bumping; True when adopted.

    Trust comes only from the kernel side-channel proof attached by
    ``kernel_events._emit_tool_results`` (``event_proof``). Display
    ``event.metadata`` workspace keys are never consulted for trust.
    """
    from codey.operations.kernel_provenance import event_proof

    proof = event_proof(event)
    if proof is None:
        return False
    identity = proof.identity
    rev = identity.revision
    fp = identity.fingerprint
    work.workspace_revision = rev
    work.workspace_fingerprint = fp
    with suppress(Exception):
        work.evidence.set_workspace_state(rev, fp)
    return True
