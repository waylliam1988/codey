"""Shell approval tickets, execution, and continuation.

The approval card pauses the model; ``claim_shell_ticket`` atomically consumes
the approval and mints a spawn ticket, ``execute_shell_ticket`` runs it with a
final Stop check under the spawn gate. Import-light by design (see
``test_server_lazy_state``): nothing here may load the browser stack,
research, or concepts at import time.
"""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from codey.agents.shell_approval import render_deferred_tool_calls
from codey.app.run_registry import RunSnapshot
from codey.operations.task_context import safe_verification_candidates
from codey.operations.task_state import TaskState
from codey.policies.limits import SHELL_OUTPUT_LIMIT, SHELL_TIMEOUT
from codey.policies.shell_followup import ShellFollowupInput, render_shell_followup
from codey.providers.catalog import DEFAULT_PROVIDER_ID
from codey.runtime.core import cancellation
from codey.utils.text_budget import clip_middle
from codey.workspace.setup_context import safe_setup_context


@dataclass(frozen=True)
class ShellApprovalContinuationPlan:
    continuation: str
    provider_id: str


@dataclass(frozen=True)
class ShellExecutionTicket:
    """Single-use, immutable shell spawn authorization.

    Minted under the claim lock by ``claim_shell_ticket``: generation, stop
    flag, resolved cwd, and approval consumption happen together, so an
    Allow cannot outrun a concurrent Stop. The execution layer only accepts
    this ticket and re-validates it under the same lock immediately before
    Popen (spawn gate).
    """

    command: str
    cwd: Path
    generation: int
    timeout: int
    output_limit: int


def _approval_generation_current(ctx: TaskState, expected: int) -> bool:
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


def mint_shell_ticket(
    *,
    lock,
    approvals,
    run_registry,
    approval_id: str,
    timeout: int,
    output_limit: int,
) -> tuple[dict | None, ShellExecutionTicket | None]:
    """Atomically consume an approval and mint a spawn ticket.

    Pop, generation snapshot, stop-flag snapshot, and cwd resolution happen
    under one ``lock`` hold so Stop cannot invalidate the claim between
    steps. Returns ``(pending, ticket)``; ``ticket`` is None when the claim
    is stale/stopped or the cwd fails closed. ``pending`` is still returned
    for stopped-denial event recording.
    """

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


def claim_shell_ticket(
    ctx: TaskState,
    approval_id: str,
    *,
    timeout: int,
    output_limit: int,
) -> tuple[dict | None, ShellExecutionTicket | None]:
    """Claim an approval against Stop under the spawn gate.

    Without the gate here, a Stop landing between pop and the generation
    snapshot would mint a ticket Stop already meant to kill. Lock order is
    always gate->lock.
    """
    with ctx._shell_spawn_gate:
        return mint_shell_ticket(
            lock=ctx.lock,
            approvals=ctx.approvals,
            run_registry=ctx.run_registry,
            approval_id=approval_id,
            timeout=timeout,
            output_limit=output_limit,
        )


def _shell_gate(ctx: TaskState):
    """Spawn gate shared with the Stop path.

    Production ``ctx._shell_spawn_gate`` is also held while Stop bumps the
    approval generation, so holding it across final-check+Popen closes the
    window. It is deliberately *not* ``ctx.lock``: the final check calls
    ``ctx.approval_generation()``, which takes ``ctx.lock`` itself, so gating
    on ``ctx.lock`` would self-deadlock. Test doubles without a gate fall
    back to a private lock (no cross-path atomicity, same code path).
    """
    gate = getattr(ctx, "_shell_spawn_gate", None)
    if gate is not None:
        return gate
    return threading.Lock()


def execute_shell_ticket(ctx: TaskState, ticket: ShellExecutionTicket) -> dict:
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
    ctx: TaskState,
    project: str | Path,
    rel: str,
    command: str,
    *,
    timeout: int | None = None,
    output_limit: int | None = None,
    expected_approval_generation: int | None = None,
) -> dict:
    """Direct-execution entry (tests, headless). Approval-card flow must use
    ``shell_service.claim_shell_ticket`` + ``execute_shell_ticket`` instead."""
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
    "ShellApprovalContinuationPlan",
    "ShellExecutionTicket",
    "build_shell_approval_continuation",
    "build_shell_approval_continuation_plan",
    "claim_shell_ticket",
    "execute_approved_shell",
    "execute_shell_ticket",
    "mint_shell_ticket",
    "safe_project_cwd",
    "shell_continuation_setup_context",
    "shell_followup_hints",
    "shell_followup_verification_candidates",
]
