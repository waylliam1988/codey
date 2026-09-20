from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from codey.research.advisors import EvidencePack

from codey.agents.consensus import (
    ConsensusAdvice,
    ConsensusResult,
    run_consensus as run_consensus_core,
    run_project_audit as run_project_audit_core,
)
from codey.agents.shell_approval import render_deferred_tool_calls
from codey.app import provider_services as _provider_services
from codey.automation.browser_worker import submit as submit_browser_task
from codey.policies.limits import REVIEW_TIMEOUT, SHELL_OUTPUT_LIMIT, SHELL_TIMEOUT
from codey.policies.shell_followup import ShellFollowupInput, render_shell_followup
from codey.providers.catalog import DEFAULT_PROVIDER_ID, PROVIDER_LABELS
from codey.providers import controls as provider_controls
from codey.reviews.core import ReviewResult, parse_review_with_repair, render_review_prompt
from codey.reviews.impact_map import safe_review_impact_map
from codey.runtime.core import cancellation
from codey.runtime.observe.prompt_envelope import FailOpenPromptTrace, record_provider_send_prompt
from codey.app.run_registry import RunSnapshot
from codey.storage.managed_outputs import ManagedOutputStore
from codey.utils.refs import clip, digest_text
from codey.utils.text_budget import clip_middle
from codey.workspace.setup_context import safe_setup_context
from codey.operations.task_context import safe_verification_candidates


@dataclass(frozen=True)
class ShellApprovalContinuationPlan:
    continuation: str
    provider_id: str


# Single entry point lives in provider_services. These wrappers resolve it at
# call time (not import time) so ``mock.patch.object`` on either module takes
# effect; behavior and availability-cache state stay shared.
def _provider_registry():
    return _provider_services._provider_registry()


def provider_tab_availability() -> dict:
    return _provider_services.provider_tab_availability()


def warm_provider_tabs(*args: object, **kwargs: object) -> dict:
    return _provider_services.warm_provider_tabs(*args, **kwargs)


def connect_existing_provider(provider_id: str) -> object:
    return _provider_services.connect_existing_provider(provider_id)


def connect_fresh_provider_tab(provider_id: str, **kwargs: object) -> object:
    return _provider_services.connect_fresh_provider_tab(provider_id, **kwargs)


def borrow_open_provider(provider_id: str, owner_page: object) -> object | None:
    return _provider_services.borrow_open_provider(provider_id, owner_page)


def reviewer_candidates(
    ctx: Any,
    writer_id: str,
    *,
    supervisor: object | None = None,
) -> tuple[str, ...]:
    return _provider_services.reviewer_candidates(ctx, writer_id, supervisor=supervisor)


def provider_availability(ctx: Any) -> dict[str, bool]:
    return _provider_services.provider_availability(ctx)


def provider_availability_from_statuses(
    ctx: Any,
    statuses: dict[str, bool],
) -> dict[str, bool]:
    return _provider_services.provider_availability_from_statuses(ctx, statuses)


def provider_payload(statuses: dict[str, bool] | None = None) -> list[dict]:
    return _provider_services.provider_payload(statuses)


def provider_catalog() -> list[dict]:
    return _provider_services.provider_catalog()


def provider_status_update(provider_id: str, available: bool) -> list[dict]:
    return _provider_services.provider_status_update(provider_id, available)


def reset_provider_availability_cache() -> None:
    return _provider_services.reset_provider_availability_cache()


def _note_availability_statuses(raw: dict[str, bool], now: float) -> None:
    return _provider_services._note_availability_statuses(raw, now)


PROVIDER_AVAILABILITY_TTL_S = _provider_services.PROVIDER_AVAILABILITY_TTL_S
_AVAIL_LOCK = _provider_services._AVAIL_LOCK
_AVAIL_STATUSES = _provider_services._AVAIL_STATUSES
_AVAIL_AT = _provider_services._AVAIL_AT


def review_label(provider_id: str) -> str:
    return PROVIDER_LABELS.get(provider_id, provider_id)


def run_provider_warmup(ctx: Any, runner=None) -> None:
    import time as _time

    if runner is None:
        runner = warm_provider_tabs
    try:
        raw_statuses = runner()
        _note_availability_statuses(dict(raw_statuses), _time.monotonic())
        statuses = provider_availability_from_statuses(ctx, raw_statuses)
        ctx.emit({"type": "providers", "providers": provider_payload(statuses)})
    except Exception as exc:
        text = f"{type(exc).__name__}: {exc}"
        try:
            ctx.emit({
                "type": "status",
                "status": "Provider warmup failed",
                "detail": clip(text, 240),
                "error_ref": digest_text(text)[:24],
            })
        except Exception:
            return


def start_provider_warmup(ctx: Any, runner=None, *, delay_s: float = 0.0) -> None:
    """Queue warmup; serve() passes a short delay to stay off the boot path."""
    import threading as _threading

    def _delayed() -> None:
        submit_browser_task(run_provider_warmup, ctx, runner)

    if delay_s <= 0:
        _delayed()
        return
    timer = _threading.Timer(delay_s, _delayed)
    timer.daemon = True
    timer.start()


def emit_review(ctx: Any, session_id: str, text: str) -> None:
    ctx.emit({"type": "review", "session_id": session_id, "text": text})


def run_review_attempt(
    ctx: Any,
    *,
    session_id: str,
    project: str,
    task: str,
    writer_summary: str,
    changes: dict,
    recent_log: str,
    change_brief: str,
    project_map: str,
    verification_map: str,
    review_impact_map: str,
    execution_evidence: str,
    reviewer_id: str,
    reviewer,
    self_review: bool,
    trace_recorder: object | None = None,
) -> tuple[str, ReviewResult]:
    try:
        reviewer.new_chat()
        prompt = render_review_prompt(
            project=project,
            task=task,
            writer_summary=writer_summary,
            changes=changes,
            recent_log=recent_log,
            change_brief=change_brief,
            project_map=project_map,
            verification_map=verification_map,
            review_impact_map=review_impact_map,
            execution_evidence=execution_evidence,
        )
        trace = FailOpenPromptTrace(trace_recorder)
        trace.call("record_permission_profile", "reviewer", phase="review")
        record_provider_send_prompt(
            trace_recorder,
            name="review_prompt",
            text=prompt,
            purpose="review prompt sent to provider",
            source_ref="provider_send:review",
            capability_id="review_runner",
        )
        with provider_controls.suppress_assistance():
            reply = reviewer.send(prompt, timeout=REVIEW_TIMEOUT)

            def send_repair_prompt(repair: str) -> str:
                record_provider_send_prompt(
                    trace_recorder,
                    name="review_repair_prompt",
                    text=repair,
                    purpose="review repair prompt sent to provider",
                    source_ref="provider_send:review_repair",
                    capability_id="review_runner",
                )
                return reviewer.send(repair, timeout=REVIEW_TIMEOUT)

            review = parse_review_with_repair(
                reply,
                send_repair_prompt,
                changes=changes,
            )
        label = review_label(reviewer_id)
        prefix = f"{label} self-review" if self_review else label
        if review.approved:
            emit_review(ctx, session_id, f"{prefix} approved")
        else:
            emit_review(ctx, session_id, f"{prefix} suggested changes")
        return reviewer_id, review
    finally:
        try:
            reviewer.close()
        except Exception:
            pass


def run_review(
    ctx: Any,
    *,
    session_id: str,
    project: str,
    task: str,
    writer_summary: str,
    changes: dict,
    recent_log: str,
    writer_id: str,
    change_brief: str = "",
    project_map: str = "",
    verification_map: str = "",
    review_impact_map: str | None = None,
    execution_evidence: str = "",
    trace_recorder: object | None = None,
) -> tuple[str, ReviewResult] | None:
    cancellation.check()
    last_error: Exception | None = None
    if review_impact_map is None:
        review_impact_map = safe_review_impact_map(project, changes)
    for reviewer_id in reviewer_candidates(ctx, writer_id):
        cancellation.check()
        try:
            reviewer = connect_existing_provider(reviewer_id)
            ctx.set_provider_session(reviewer_id, None)
            return run_review_attempt(
                ctx,
                session_id=session_id,
                project=project,
                task=task,
                writer_summary=writer_summary,
                changes=changes,
                recent_log=recent_log,
                change_brief=change_brief,
                project_map=project_map,
                verification_map=verification_map,
                review_impact_map=review_impact_map,
                execution_evidence=execution_evidence,
                reviewer_id=reviewer_id,
                reviewer=reviewer,
                self_review=False,
                trace_recorder=trace_recorder,
            )
        except cancellation.TaskCancelled:
            raise
        except Exception as exc:
            last_error = exc
    cancellation.check()
    try:
        reviewer_id = (writer_id or DEFAULT_PROVIDER_ID).strip().lower()
        reviewer = connect_fresh_provider_tab(reviewer_id)
        return run_review_attempt(
            ctx,
            session_id=session_id,
            project=project,
            task=task,
            writer_summary=writer_summary,
            changes=changes,
            recent_log=recent_log,
            change_brief=change_brief,
            project_map=project_map,
            verification_map=verification_map,
            review_impact_map=review_impact_map,
            execution_evidence=execution_evidence,
            reviewer_id=reviewer_id,
            reviewer=reviewer,
            self_review=True,
            trace_recorder=trace_recorder,
        )
    except cancellation.TaskCancelled:
        raise
    except Exception as exc:
        last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("no review model available")


def connect_consensus_provider(selected_provider, provider_id: str):
    """Use an already-open sibling tab while a Writer provider is active."""

    if provider_id == "local":
        return connect_existing_provider(provider_id)
    owner_page = getattr(getattr(selected_provider, "session", None), "page", None)
    if owner_page is not None:
        helper = borrow_open_provider(provider_id, owner_page)
        if helper is None:
            raise RuntimeError(f"{review_label(provider_id)} tab is not open in this browser context")
        return helper
    return connect_existing_provider(provider_id)


def run_consensus(
    ctx: Any,
    *,
    selected_provider,
    selected_provider_id: str,
    task: str,
    context: str = "",
    draft: str = "",
    plan: bool = False,
    draft_first: bool = False,
    owner_prompt: str = "",
    trace_recorder: object | None = None,
) -> ConsensusResult | None:
    return run_consensus_core(
        selected_provider=selected_provider,
        selected_provider_id=selected_provider_id,
        task=task,
        provider_ids=tuple(PROVIDER_LABELS),
        provider_labels=PROVIDER_LABELS,
        availability=lambda: provider_availability(ctx),
        connect_existing=lambda provider_id: connect_consensus_provider(
            selected_provider,
            provider_id,
        ),
        clear_provider_session=lambda provider_id: ctx.set_provider_session(provider_id, None),
        context=context,
        draft=draft,
        plan=plan,
        draft_first=draft_first,
        owner_prompt=owner_prompt,
        trace_recorder=trace_recorder,
    )


def run_project_audit(
    ctx: Any,
    *,
    project: str | Path,
    selected_provider=None,
    selected_provider_id: str,
    task: str,
    context: str = "",
    trace_recorder: object | None = None,
) -> tuple[ConsensusAdvice, ...]:
    return run_project_audit_core(
        project=project,
        selected_provider_id=selected_provider_id,
        task=task,
        provider_ids=tuple(PROVIDER_LABELS),
        provider_labels=PROVIDER_LABELS,
        availability=lambda: provider_availability(ctx),
        connect_existing=lambda provider_id: connect_consensus_provider(
            selected_provider,
            provider_id,
        ),
        clear_provider_session=lambda provider_id: ctx.set_provider_session(provider_id, None),
        context=context,
        trace_recorder=trace_recorder,
    )


def run_research_advisors(
    ctx: Any,
    *,
    selected_provider,
    selected_provider_id: str,
    pack: EvidencePack,
) -> tuple[ConsensusAdvice, ...]:
    from codey.research.advisors import run_research_advisors as run_research_advisors_core

    return run_research_advisors_core(
        selected_provider_id=selected_provider_id,
        provider_ids=tuple(PROVIDER_LABELS),
        provider_labels=PROVIDER_LABELS,
        availability=lambda: provider_availability(ctx),
        connect_existing=lambda provider_id: connect_consensus_provider(
            selected_provider,
            provider_id,
        ),
        clear_provider_session=lambda provider_id: ctx.set_provider_session(provider_id, None),
        pack=pack,
    )


def _approval_generation_current(ctx: Any, expected: int) -> bool:
    try:
        current = int(ctx.approval_generation())
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        raise
    except Exception:
        # Fail closed: an unreadable epoch must stop execution, never approve.
        return False
    return int(expected or 0) == int(current or 0)


def _stopped_shell_result() -> dict:
    """Refused-before/during-execution result: never approved, never continued."""
    return {
        "ok": False,
        "status": "stopped",
        "error": "command stopped",
        "exit_code": None,
        "output": "",
        "stopped": True,
    }


def safe_project_cwd(project: str | Path, rel: str) -> Path:
    root = Path(project).expanduser().resolve()
    cwd = (root / (rel or ".")).resolve()
    if root not in cwd.parents and cwd != root:
        raise ValueError("cwd escapes project root")
    try:
        if cwd.is_symlink():
            raise ValueError("cwd is a symlink")
    except OSError as exc:
        raise ValueError("cwd is not a directory") from exc
    if not cwd.is_dir():
        raise ValueError("cwd is not a directory")
    return cwd


@dataclass(frozen=True)
class ShellExecutionTicket:
    """Single-use, immutable shell spawn authorization.

    Minted under ``ctx.lock`` by ``AppContext.claim_shell_ticket``: generation,
    stop flag, resolved cwd, and approval consumption happen together, so an
    Allow cannot outrun a concurrent Stop. The execution layer only accepts
    this ticket and re-validates it under the same lock immediately before
    Popen (spawn gate)."""

    command: str
    cwd: Path
    generation: int
    timeout: int
    output_limit: int


def mint_shell_ticket(
    *,
    lock: Any,
    approvals: Any,
    run_registry: Any,
    approval_id: str,
    timeout: int,
    output_limit: int,
) -> tuple[dict | None, ShellExecutionTicket | None]:
    """Atomically consume an approval and mint a spawn ticket.

    Pop, generation snapshot, stop-flag snapshot, and cwd resolution happen
    under one ``lock`` hold so Stop cannot invalidate the claim between
    steps. Returns ``(pending, ticket)``; ``ticket`` is None when the claim
    is stale/stopped or the cwd fails closed. ``pending`` is still returned
    for stopped-denial event recording."""

    with lock:
        pending = approvals.pop_shell(approval_id)
        if pending is None:
            return None, None
        try:
            claimed_generation = int(pending.pop("_approval_generation", 0) or 0)
        except (TypeError, ValueError):
            claimed_generation = 0
        current_generation = approvals.current_generation()
        try:
            stop_set = bool(run_registry.stop_flag.is_set())
        except Exception:
            return pending, None
        if stop_set or int(claimed_generation or 0) != int(current_generation or 0):
            return pending, None
        try:
            cwd = safe_project_cwd(
                str(pending.get("project") or ""),
                str(pending.get("cwd") or "."),
            )
        except Exception:
            return pending, None
        ticket = ShellExecutionTicket(
            command=str(pending.get("command") or ""),
            cwd=cwd,
            generation=int(claimed_generation or 0),
            timeout=int(timeout),
            output_limit=int(output_limit),
        )
        return pending, ticket


def _shell_gate(ctx: Any) -> Any:
    """Spawn gate shared with the Stop path.

    Production ``AppContext._shell_spawn_gate`` is also held while Stop bumps
    the approval generation, so holding it across final-check+Popen closes
    the window. It is deliberately *not* ``ctx.lock``: the final check calls
    ``ctx.approval_generation()``, which takes ``ctx.lock`` itself, so
    gating on ``ctx.lock`` would self-deadlock. Test doubles without a gate
    fall back to a private lock (no cross-path atomicity, same code path)."""
    gate = getattr(ctx, "_shell_spawn_gate", None)
    if gate is not None:
        return gate
    return threading.Lock()


def execute_shell_ticket(ctx: Any, ticket: ShellExecutionTicket) -> dict:
    """Execute an already-claimed ticket. Final Stop check and Popen happen
    under the spawn gate; waiting happens outside the gate."""
    command = (ticket.command or "").strip()
    if not command:
        return {
            "ok": False,
            "status": "spawn_error",
            "error": "command required",
            "exit_code": None,
            "output": "",
        }
    try:
        gate = _shell_gate(ctx)
        with gate:
            if not _approval_generation_current(ctx, ticket.generation):
                return _stopped_shell_result()
            try:
                stop_flag = ctx.run_registry.stop_flag
            except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
                raise
            except Exception:
                stop_flag = None
            try:
                stop_set = bool(stop_flag.is_set()) if stop_flag is not None else False
            except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
                raise
            except Exception:
                stop_set = False
            if stop_set:
                return _stopped_shell_result()
            # Commit point: Popen while still holding the gate so a Stop that
            # needs the same gate to bump the generation cannot interleave.
            with cancellation.scope(stop_flag):
                proc, job = cancellation.start_process(
                    command,
                    cwd=ticket.cwd,
                    shell=True,
                )
        stop_flag_after = getattr(
            getattr(ctx, "run_registry", None), "stop_flag", None
        )
        with cancellation.scope(stop_flag_after):
            completed = cancellation.wait_process(proc, job, command, ticket.timeout)
            proc = completed
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        return _stopped_shell_result()
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "status": "timeout",
            "error": f"command timed out after {ticket.timeout}s",
            "exit_code": None,
            "output": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "spawn_error",
            "error": str(exc),
            "exit_code": None,
            "output": "",
        }

    output_parts = []
    if proc.stdout:
        output_parts.append(proc.stdout.rstrip())
    if proc.stderr:
        output_parts.append("[stderr]\n" + proc.stderr.rstrip())
    output = "\n\n".join(output_parts) or "(no output)"
    output, truncated = clip_middle(output, ticket.output_limit)
    return {
        "ok": True,
        "status": "exit",
        "error": None,
        "exit_code": proc.returncode,
        "output": output,
        "truncated": truncated,
    }


def execute_approved_shell(
    ctx: Any,
    project: str | Path,
    rel: str,
    command: str,
    *,
    timeout: int | None = None,
    output_limit: int | None = None,
    expected_approval_generation: int | None = None,
) -> dict:
    """Direct-execution entry (tests, headless). Approval-card flow must use
    ``AppContext.claim_shell_ticket`` + ``execute_shell_ticket`` instead."""
    command = (command or "").strip()
    if not command:
        return {
            "ok": False,
            "status": "spawn_error",
            "error": "command required",
            "exit_code": None,
            "output": "",
        }
    resolved_timeout = SHELL_TIMEOUT if timeout is None else timeout
    resolved_limit = SHELL_OUTPUT_LIMIT if output_limit is None else output_limit
    try:
        cwd = safe_project_cwd(project, rel)
    except Exception as exc:
        return {
            "ok": False,
            "status": "spawn_error",
            "error": str(exc),
            "exit_code": None,
            "output": "",
        }
    generation: int | None = expected_approval_generation
    if generation is None:
        try:
            generation = int(ctx.approval_generation())
        except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
            return _stopped_shell_result()
        except Exception:
            generation = 0
    ticket = ShellExecutionTicket(
        command=command,
        cwd=cwd,
        generation=int(generation or 0),
        timeout=resolved_timeout,
        output_limit=resolved_limit,
    )
    return execute_shell_ticket(ctx, ticket)


def build_shell_approval_continuation(
    *,
    command: str,
    result: dict,
    post_approval_instructions: str = "",
    setup_context: str = "",
    followup_hints: str = "",
    deferred_tool_calls: tuple[dict[str, object], ...] = (),
) -> str:
    truncation_note = (
        "\nShell output was truncated. Do not assume omitted content "
        "is clean; inspect narrower output if needed.\n"
        if result.get("truncated")
        else ""
    )
    checklist = (post_approval_instructions or "").strip()
    checklist_block = f"{checklist}\n\n" if checklist else ""
    setup_block = f"{setup_context.strip()}\n\n" if setup_context.strip() else ""
    followup_block = f"{followup_hints.strip()}\n\n" if followup_hints.strip() else ""
    deferred = render_deferred_tool_calls(deferred_tool_calls)
    deferred_block = f"{deferred}\n\n" if deferred else ""
    return (
        "Continue the interrupted task in this same conversation.\n"
        "The user approved and ran this shell command:\n"
        f"{command}\n\n"
        f"Exit code: {result.get('exit_code')}\n"
        "Output:\n"
        f"{result.get('output') or result.get('error') or '(no output)'}\n\n"
        f"{truncation_note}"
        f"{setup_block}"
        f"{checklist_block}"
        f"{followup_block}"
        f"{deferred_block}"
        "Use this result to continue the original task. If the task is complete,"
        " reply with a JSON done tool call."
    )


def build_shell_approval_continuation_plan(
    *,
    pending: dict,
    result: dict,
    active_run: RunSnapshot | None = None,
) -> ShellApprovalContinuationPlan:
    setup_context = shell_continuation_setup_context(pending)
    followup_hints = shell_followup_hints(
        pending=pending,
        result=result,
    )
    continuation = build_shell_approval_continuation(
        command=str(pending.get("command") or ""),
        result=result,
        post_approval_instructions=str(
            pending.get("post_approval_instructions") or ""
        ),
        setup_context=setup_context,
        followup_hints=followup_hints,
        deferred_tool_calls=tuple(
            item
            for item in pending.get("deferred_tool_calls", ())
            if isinstance(item, dict)
        ),
    )
    active_provider = (
        active_run.provider_id
        if active_run is not None
        and active_run.run_id == str(pending.get("run_id") or "")
        and active_run.session_id == str(pending.get("session_id") or "")
        else ""
    )
    provider_id = active_provider or str(pending.get("provider") or DEFAULT_PROVIDER_ID)
    return ShellApprovalContinuationPlan(
        continuation=continuation,
        provider_id=provider_id,
    )


def shell_continuation_setup_context(pending: dict) -> str:
    if pending.get("risk_label") not in {
        "dependency_install",
        "system_install",
        "external_source",
        "dev_server",
    }:
        return ""
    project = str(pending.get("project") or "").strip()
    if not project:
        return ""
    return safe_setup_context(project)


def shell_followup_verification_candidates(project: str | Path, risk_label: object):
    if risk_label not in {"dependency_install", "dev_server", "publish"}:
        return ()
    return safe_verification_candidates(project)


def shell_followup_hints(
    *,
    pending: dict,
    result: dict,
) -> str:
    project = str(pending.get("project") or "").strip()
    return render_shell_followup(ShellFollowupInput(
        risk_label=str(pending.get("risk_label") or "generic"),
        exit_code=result.get("exit_code"),
        output=str(result.get("output") or result.get("error") or ""),
        truncated=bool(result.get("truncated")),
        verification_candidates=shell_followup_verification_candidates(
            project,
            pending.get("risk_label"),
        ),
    ))


__all__ = [
    "ManagedOutputStore",
    "build_shell_approval_continuation",
    "build_shell_approval_continuation_plan",
    "execute_approved_shell",
    "provider_availability",
    "provider_availability_from_statuses",
    "provider_catalog",
    "provider_payload",
    "provider_status_update",
    "reset_provider_availability_cache",
    "review_label",
    "reviewer_candidates",
    "run_consensus",
    "run_project_audit",
    "run_provider_warmup",
    "run_research_advisors",
    "run_review",
    "run_review_attempt",
    "shell_continuation_setup_context",
    "shell_followup_hints",
    "shell_followup_verification_candidates",
    "ShellApprovalContinuationPlan",
    "start_provider_warmup",
]
