"""Project completion orchestration.

Keeps only orchestration; phase logic lives in
project_completion_context / project_writer_phase /
project_review_phase / project_completion_enforcement.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from codey.agents.consensus import render_project_context
from codey.knowledge.brief import KnowledgeBriefBuilder
from codey.knowledge.note import KnowledgeNote
from codey.operations.context import RunFrame, RunHooks, RunWork
from codey.operations.project_completion_context import (
    COMPLETION_REPAIR_FOLLOWUP,
    MAX_COMPLETION_REPAIR_ROUNDS,
    NEW_PROJECT_IGNORED_DIRS,
    NEW_PROJECT_IGNORED_FILES,
    AgentAccess,
    PersistenceAccess,
    ProjectCompletionDeps,
    ProjectRun,
    ProjectRuntimeMutationError,
    ReviewAccess,
    RuntimeAccess,
    VerificationAccess,
    _bullet_lines,
    _ProjectRun,
    blocked_result,
    managed_tool_fns,
    project_has_user_files,
    record_completion_proof_trace,
    record_edit_integrity_trace,
    record_review_input_prepared_trace,
    safe_verification_map,
)
from codey.operations.project_completion_enforcement import enforce_completion
from codey.operations.project_review_phase import run_review_phase
from codey.operations.project_writer_phase import run_writer_phase
from codey.operations.prompting import (
    record_secondary_input_prepared_trace as _record_secondary_input_prepared_trace,
)
from codey.operations.research_flow import research_payload as _research_payload
from codey.operations.result import ModeOutcome
from codey.operations.task_context import ProjectTaskContextBuilder
from codey.research.analysis_run import analysis_run_record
from codey.research.artifact_lineage import artifact_ref_from_managed_output
from codey.research.reproducibility import build_reproducibility_capsule
from codey.runs.receipt import VERIFICATION_TRUST_TRUSTED, build_task_receipt
from codey.runs.trace_schema import MAX_ANALYSIS_RUNS, MAX_ARTIFACT_REFS
from codey.runs.work_checkpoint import (
    WorkCheckpoint,
    WorkCheckpointStore,
)
from codey.runtime.core import cancellation
from codey.runtime.observe.events import RunEvent
from codey.runtime.observe.terminalizer import task_done_event
from codey.task.model import execution_task
from codey.workspace.change_brief import (
    new_project_change_brief,
    project_audit_change_brief,
)
from codey.workspace.config import ProjectConfigLoadResult

__all__ = [
    "AgentAccess",
    "COMPLETION_REPAIR_FOLLOWUP",
    "MAX_COMPLETION_REPAIR_ROUNDS",
    "NEW_PROJECT_IGNORED_DIRS",
    "NEW_PROJECT_IGNORED_FILES",
    "PersistenceAccess",
    "ProjectCompletionDeps",
    "ProjectRun",
    "ProjectRuntimeMutationError",
    "ReviewAccess",
    "RuntimeAccess",
    "VerificationAccess",
    "_ProjectRun",
    "blocked_result",
    "handle_project_tool_event",
    "managed_tool_fns",
    "project_has_user_files",
    "record_analysis_run",
    "record_completion_proof_trace",
    "record_edit_integrity_trace",
    "record_project_memory",
    "record_review_input_prepared_trace",
    "run_project_mode",
    "safe_verification_map",
]


def _prepare_project_context(ctx: ProjectRun) -> None:
    ctx.state = ctx.deps.state
    ctx.request = ctx.frame.request
    ctx.agent_task = execution_task(ctx.request)
    ctx.agent_fresh_chat = ctx.frame.fresh_chat
    if ctx.work.ledger is not None:
        ctx.work.record_agent_events_in_ledger = True
    ctx.context_builder = ProjectTaskContextBuilder(
        project_facts=ctx.deps.persistence.project_facts,
        work_checkpoints=ctx.deps.persistence.work_checkpoints,
        knowledge_store=ctx.deps.persistence.knowledge_store,
        config_result=ctx.config_result,
    )
    ctx.project_context = ctx.context_builder.build(
        project=ctx.project,
        task=ctx.request.task,
        session_id=ctx.request.session_id,
        run_id=ctx.frame.run_id,
        continue_task=ctx.request.continue_task,
        provider_session_changed=ctx.frame.provider_session_changed,
    )
    ctx.verified_facts = ctx.project_context.verified_facts
    ctx.verification_verified_commands = (
        ctx.project_context.verification_verified_commands
    )
    ctx.verification_candidates = ctx.project_context.verification_candidates
    ctx.resumed_verification_commands = (
        ctx.project_context.resumed_verification_commands
    )
    ctx.configured_verification_commands = (
        ctx.project_context.configured_verification_commands
    )
    ctx.configured_ignored_paths = ctx.project_context.configured_ignored_paths
    ctx.project_map_chars = ctx.project_context.project_map_chars
    ctx.project_map = ctx.project_context.project_map
    ctx.work.work_checkpoint = ctx.project_context.checkpoint.item
    ctx.checkpoint_prompt = ctx.project_context.checkpoint.prompt
    ctx.resumed_changed_files = ctx.project_context.checkpoint.changed_files
    ctx.resumed_successful_checks = ctx.project_context.checkpoint.successful_checks
    ctx.work.evidence.seed_checks(ctx.project_context.checkpoint.seed_checks)
    if ctx.project_context.checkpoint.workspace_changed:
        ctx.work.advance_workspace_revision(
            ctx.deps.verification.workspace_revisions,
            ctx.project,
            ignored_paths=ctx.configured_ignored_paths,
        )
    _prepare_new_project_context(ctx)
    key = str(Path(ctx.project).expanduser().resolve())
    ctx.tracker = ctx.state.change_tracker_for(
        key,
        persistent=not ctx.deps.agent.is_git_repository(key),
    )
    ctx.tried_writers = set(ctx.frame.preflight_tried)


def _prepare_new_project_context(ctx: ProjectRun) -> None:
    ctx.has_user_files = project_has_user_files(ctx.project)
    if ctx.deps.agent.run_consensus is not None and not ctx.has_user_files:
        context = render_project_context(
            ctx.frame.conversation.snapshot,
            ctx.verified_facts,
            project_map=ctx.project_map,
        )
        try:
            _record_secondary_input_prepared_trace(
                ctx.frame.trace,
                "consensus",
                task=ctx.request.task,
                context=context,
            )
            planned = ctx.deps.agent.run_consensus(
                selected_provider=ctx.frame.provider,
                selected_provider_id=ctx.frame.provider_id,
                task=ctx.request.task,
                context=context,
                plan=True,
                draft_first=True,
                trace_recorder=ctx.frame.trace,
            )
        except cancellation.TaskCancelled:
            raise
        except Exception:
            ctx.state.set_provider_session(ctx.frame.provider_id, None)
            ctx.agent_fresh_chat = True
            planned = None
        if planned is not None:
            ctx.change_brief = new_project_change_brief(ctx.request.task, planned.answer)
            ctx.agent_task = ctx.change_brief.apply_to_task(execution_task(ctx.request))
    elif ctx.deps.agent.run_project_audit is not None and ctx.has_user_files:
        context = render_project_context(
            ctx.frame.conversation.snapshot,
            ctx.verified_facts,
            project_map=ctx.project_map,
        )
        try:
            _record_secondary_input_prepared_trace(
                ctx.frame.trace,
                "project_audit",
                task=ctx.request.task,
                context=context,
            )
            reports = ctx.deps.agent.run_project_audit(
                project=ctx.project,
                selected_provider=ctx.frame.provider,
                selected_provider_id=ctx.frame.provider_id,
                task=ctx.request.task,
                context=context,
                trace_recorder=ctx.frame.trace,
            )
        except cancellation.TaskCancelled:
            raise
        except Exception:
            reports = ()
        if reports:
            ctx.change_brief = project_audit_change_brief(ctx.request.task, reports)
            ctx.agent_task = ctx.change_brief.apply_to_task(execution_task(ctx.request))
            ctx.used_project_audit = True


def _persist_verified_project_facts(ctx: ProjectRun) -> bool:
    """Persist project facts for a trusted done run; returns success."""
    assert ctx.result is not None
    required = (
        ctx.deps.persistence.project_facts is not None
        and ctx.result.stop_reason == "done"
        and ctx.task_changed
        and ctx.result.checks_passed
        and ctx.receipt.verification.trust == VERIFICATION_TRUST_TRUSTED
        and ctx.work.evidence.has_successful_checks
        and ctx.files
    )
    if not required:
        return True
    try:
        fact_task = (
            ctx.work.work_checkpoint.original_task
            if ctx.project_context.checkpoint.resumed
            and ctx.work.work_checkpoint is not None
            else ctx.request.task
        )
        succeeded = ctx.deps.persistence.project_facts.record_successful_change(
            ctx.project,
            task=fact_task,
            files=ctx.files,
            checks=ctx.work.evidence.successful_checks,
            receipt=ctx.receipt.display.summary,
        )
    except (OSError, ValueError):
        return False
    if succeeded:
        record_project_memory(
            ctx.deps,
            project=ctx.project,
            session_id=ctx.request.session_id,
            task=ctx.request.task,
            files=ctx.files,
            receipt=ctx.receipt.display.summary,
            checks=ctx.work.evidence.successful_checks,
        )
    return bool(succeeded)


def _settle_work_checkpoint(ctx: ProjectRun, facts_write_succeeded: bool) -> None:
    if ctx.deps.persistence.work_checkpoints is None or ctx.work.work_checkpoint is None:
        return
    assert ctx.result is not None
    if ctx.result.stop_reason == "done" and facts_write_succeeded:
        try:
            ctx.deps.persistence.work_checkpoints.delete(ctx.request.session_id)
            ctx.work.work_checkpoint = None
        except OSError:
            pass
    elif ctx.result.stop_reason != "done":
        ctx.hooks.update_checkpoint(
            lambda store, item: store.set_status(item, "interrupted", ctx.result.stop_reason)
        )


def _build_project_done_event(ctx: ProjectRun) -> dict:
    assert ctx.result is not None
    changes_payload = None
    if ctx.task_changed and ctx.task_changes and ctx.task_changes.get("ok"):
        changes_payload = {
            "changed_count": ctx.task_changes.get("changed_count", 0),
            "files": ctx.task_changes.get("files", [])[:3],
            "mode": ctx.task_changes.get("mode"),
            "project": ctx.project,
        }
    research_payload = None
    if ctx.research_result is not None:
        research_payload = _research_payload(
            ctx.research_result, pipeline_result=ctx.research_pipeline_result,
        )
    return task_done_event(
        run_id=ctx.frame.run_id,
        session_id=ctx.request.session_id,
        summary=ctx.result.summary,
        stop_reason=ctx.result.stop_reason,
        turns=ctx.result.turns,
        max_turns=ctx.request.max_turns,
        provider=ctx.frame.provider_id,
        mode="hybrid" if ctx.research_result is not None else "agent",
        work=ctx.work,
        changed=ctx.task_changed,
        receipt=ctx.receipt.to_dict(),
        changes=changes_payload,
        research=research_payload,
    )


def _finalize_project(ctx: ProjectRun) -> ModeOutcome:
    assert ctx.result is not None
    ctx.receipt = build_task_receipt(
        ctx.task_changes,
        decision=ctx.decision,
        integrity=ctx.integrity,
        checks_passed=ctx.result.checks_passed,
    )
    ctx.hooks.append_ledger(
        lambda ledger: ledger.append_changes_collected(
            ctx.task_changes,
            checks_passed=ctx.result.checks_passed,
            receipt=ctx.receipt.to_dict(),
        )
    )
    facts_write_succeeded = _persist_verified_project_facts(ctx)
    _settle_work_checkpoint(ctx, facts_write_succeeded)
    with contextlib.suppress(Exception):
        ctx.tracker.prune_clean()
    ctx.frame.conversation.update_snapshot(
        replace(
            ctx.frame.conversation.snapshot,
            provider_id=ctx.frame.provider_id,
            changed_files=ctx.files,
            checks_passed=ctx.result.checks_passed,
            summary=ctx.result.summary,
            blocker="" if ctx.result.stop_reason == "done" else ctx.result.summary,
        )
    )
    event = _build_project_done_event(ctx)
    return ModeOutcome(
        event,
        research_result=ctx.research_result,
        research_pipeline_result=ctx.research_pipeline_result,
    )


def run_project_mode(
    deps: ProjectCompletionDeps,
    frame: RunFrame,
    work: RunWork,
    hooks: RunHooks,
    *,
    config_result: ProjectConfigLoadResult | None = None,
    research_result=None,
    research_pipeline_result=None,
) -> ModeOutcome:
    project = frame.request.project
    if project is None:
        raise RuntimeError("project mode requires a project")
    ctx = ProjectRun(
        deps=deps,
        frame=frame,
        work=work,
        hooks=hooks,
        project=project,
        config_result=config_result,
        research_result=research_result,
        research_pipeline_result=research_pipeline_result,
    )
    _prepare_project_context(ctx)
    run_writer_phase(ctx)
    run_review_phase(ctx)
    enforce_completion(ctx)
    return _finalize_project(ctx)


def handle_project_tool_event(
    deps: ProjectCompletionDeps,
    *,
    event: RunEvent,
    project: str,
    work: RunWork,
    run_id: str,
    update_checkpoint: Callable[
        [Callable[[WorkCheckpointStore, WorkCheckpoint], WorkCheckpoint]],
        None,
    ],
) -> None:
    call = event.call
    outcome = event.outcome
    if call is None or outcome is None:
        return
    name = str(call.name or "")
    if name == "run":
        command = str(call.args.get("command") or "")
        cwd = str(call.args.get("path") or ".")
        ok = bool(outcome.ok and outcome.exit_code == 0)
        try:
            tool_index = int(event.metadata.get("tool_index") or 0)
        except (TypeError, ValueError):
            tool_index = 0
        tool_id = f"{event.turn}:{max(0, tool_index)}"
        if ok and deps.persistence.project_facts is not None:
            with contextlib.suppress(OSError, ValueError):
                deps.persistence.project_facts.record_success(project, cwd, command)
        update_checkpoint(
            lambda store, item: store.record_run(
                item,
                command=command,
                cwd=cwd,
                ok=ok,
                workspace_revision=work.workspace_revision,
                workspace_fingerprint=work.workspace_fingerprint,
            )
        )
        record_analysis_run(
            work=work,
            project=project,
            run_id=run_id,
            tool_id=tool_id,
            tool_name=name,
            command=command,
            cwd=cwd,
            ok=ok,
            outcome=outcome,
        )
    elif name == "edit" and outcome.ok and outcome.changed:
        rel = str(call.args.get("path") or "")
        update_checkpoint(lambda store, item: store.record_edit(item, rel))


def record_analysis_run(
    *,
    work: RunWork,
    project: str,
    run_id: str,
    tool_id: str,
    tool_name: str,
    command: str,
    cwd: str,
    ok: bool,
    outcome: Any,
) -> None:
    """Project one audited run-command execution into the run trace.

    Fail-open by contract: projection or trace failures never affect the
    running task, its receipt, or the model-visible tool result.
    """

    trace = work.trace
    if trace is None or not command:
        return
    try:
        audit = outcome.audit if isinstance(outcome.audit, Mapping) else {}
        # Only real executions become AnalysisRun records. Policy denials,
        # invalid cwd, and command-not-found outcomes carry no timing and
        # must stay out of the execution audit (roadmap: record existing
        # executions, not attempts).
        if not audit.get("command_started_at"):
            return
        managed = outcome.managed_output()
        record = analysis_run_record(
            {
                "run_id": run_id,
                "tool_id": tool_id,
                "tool_name": tool_name,
                "command": command,
                "cwd": cwd,
                "project": project,
                "exit_code": outcome.exit_code,
                "ok": ok,
                "started_at": audit.get("command_started_at"),
                "finished_at": audit.get("command_finished_at"),
                "duration_ms": audit.get("command_duration_ms"),
                "managed_output": dict(managed) if managed else {},
                "capture_truncated": bool(audit.get("capture_truncated")),
            }
        )
        if record is None:
            return
        record_payload = record.to_payload()
        trace.record_analysis_run(record_payload)
        work.analysis_run_payloads.append(record_payload)
        if len(work.analysis_run_payloads) > MAX_ANALYSIS_RUNS:
            del work.analysis_run_payloads[:-MAX_ANALYSIS_RUNS]

        artifact_payload: dict[str, object] | None = None
        if managed:
            artifact = artifact_ref_from_managed_output(
                {
                    **managed,
                    "origin_run_id": run_id,
                    "produced_by": record.analysis_run_id,
                }
            )
            if artifact is not None:
                artifact_payload = artifact.to_payload()
                trace.record_artifact_refs([artifact_payload])
                work.artifact_payloads.append(artifact_payload)
                if len(work.artifact_payloads) > MAX_ARTIFACT_REFS:
                    del work.artifact_payloads[:-MAX_ARTIFACT_REFS]

        capsule = build_reproducibility_capsule(
            run_id=run_id,
            analysis_runs=work.analysis_run_payloads,
            artifacts=work.artifact_payloads,
        )
        if capsule is not None:
            trace.record_reproducibility_capsule(capsule.to_payload())
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        return


def record_project_memory(
    deps: ProjectCompletionDeps,
    *,
    project: str,
    session_id: str,
    task: str,
    files: tuple[str, ...],
    receipt: str,
    checks: tuple[object, ...],
) -> None:
    if deps.persistence.knowledge_store is None:
        return
    try:
        brief = KnowledgeBriefBuilder(deps.persistence.knowledge_store).build_for_session(session_id)
        sources = [brief.synthesis_id] if brief.synthesis_id else []
        impl = KnowledgeNote.create(
            type="implementation",
            title=task[:120] or "Project implementation",
            body=(f"Implemented project task.\n\nFiles changed:\n{_bullet_lines(files)}\n\nReceipt:\n{receipt}"),
            tags=["project", "implementation", f"session:{session_id}"],
            sources=sources,
            session_id=session_id,
            project=str(Path(project).expanduser().resolve()),
        )
        deps.persistence.knowledge_store.write_note(impl)
        if checks:
            verification = KnowledgeNote.create(
                type="verification",
                title=f"Verification for {task[:80] or 'project task'}",
                body="Successful checks:\n"
                + _bullet_lines(tuple(f"{item.command} (cwd {item.cwd})" for item in checks)),
                tags=["project", "verification", f"session:{session_id}"],
                sources=[impl.id],
                session_id=session_id,
                project=str(Path(project).expanduser().resolve()),
            )
            deps.persistence.knowledge_store.write_note(verification)
            deps.persistence.knowledge_store.link(impl.id, verification.id, "verifies")
        if brief.synthesis_id:
            deps.persistence.knowledge_store.link(brief.synthesis_id, impl.id, "implements")
    except (OSError, ValueError):
        return
