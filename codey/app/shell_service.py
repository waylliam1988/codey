"""Shell approval tickets, execution, and continuation.

The approval card pauses the model; ``claim_shell_ticket`` atomically consumes
the approval and mints a spawn ticket, ``execute_shell_ticket`` runs it with a
final Stop check under the spawn gate. Import-light by design (see
``test_server_lazy_state``): nothing here may load the browser stack,
research, or concepts at import time.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    # 回答原 native 调用的结构化结果（可序列化字典）；continuation 运行
    # 以 turn-0 初始结果首发它们（同会话 native 优先，异会话文本兜底）。
    shell_results: tuple[dict[str, object], ...] = ()


def shell_result_content(*, command: str, result: dict[str, Any], approved: bool) -> str:
    """Structure the shell outcome text answered to the original call."""
    from codey.agents.shell_approval import shell_command_text

    output = str(result.get("output") or result.get("error") or "(no output)")
    exit_code = result.get("exit_code")
    if approved:
        head = (
            f"Shell result (approved and executed): {shell_command_text(command)}\n"
            f"Exit code: {exit_code}\nOutput:\n{output}"
        )
    else:
        head = (
            f"Shell result (NOT executed, denied by user): {shell_command_text(command)}\n"
            f"Output:\n{output}"
        )
    if bool(result.get("truncated")):
        head += "\n[output truncated; inspect narrower output if needed]"
    return head


def shell_result_row(pending: dict[str, Any], result: dict[str, Any], *, approved: bool) -> Any:
    """Build the turn-0 redelivery row answering the original shell call.

    原 call id 无有效值时返回 None（调用方仅走文本 continuation）。
    行标记 redelivered：重发已裁决结果，不重新执行命令。
    """
    from codey.agents.request import RecoveredToolOutcome
    from codey.agents.shell_approval import valid_shell_call_id
    from codey.runtime.core.models import ToolCall
    from codey.toolchain.runtime import ToolOutcome

    call_id = pending.get("call_id", "")
    if not valid_shell_call_id(call_id):
        return None
    command = str(pending.get("command") or "")
    cwd = str(pending.get("cwd") or ".")
    text = shell_result_content(command=command, result=result, approved=approved)
    exit_code = result.get("exit_code")
    exit_code = exit_code if type(exit_code) is int else None
    ok = approved and result.get("ok") is True and result.get("status") != "denied"
    if not approved:
        ok = False
    call = ToolCall(
        name="shell",
        args={"command": command, "path": cwd},
        call_id=call_id,
    )
    outcome = ToolOutcome(
        text, ok,
        audit={"exit_code": exit_code} if exit_code is not None else {},
        error_code="" if ok else str(result.get("status") or "denied" if not approved else "shell_error"),
        exit_code=exit_code,
    )
    return RecoveredToolOutcome(
        call=call,
        outcome=outcome,
        turn=0,
        tool_index=0,
        effect_id="",
        redelivered=True,
    )


def shell_result_payload(row: Any) -> dict[str, object] | None:
    """Serialize one shell result row for cross-boundary submission."""
    if row is None:
        return None
    try:
        call = getattr(row, "call", None)
        outcome = getattr(row, "outcome", None)
        return {
            "call_id": str(getattr(call, "call_id", "") or ""),
            "command": str((getattr(call, "args", {}) or {}).get("command") or ""),
            "cwd": str((getattr(call, "args", {}) or {}).get("path") or "."),
            "model_text": str(getattr(outcome, "model_text", "") or ""),
            "ok": bool(getattr(outcome, "ok", False)),
            "exit_code": getattr(outcome, "exit_code", None),
            "error_code": str(getattr(outcome, "error_code", "") or ""),
            "truncated": bool(getattr(outcome, "truncated", False)),
        }
    except Exception:
        return None


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


def _stopped_shell_result() -> dict[str, Any]:
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
    if project is None or str(project).strip() == "":
        raise ValueError("project required")
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
    lock: Any,
    approvals: Any,
    run_registry: Any,
    approval_id: str,
    timeout: int,
    output_limit: int,
) -> tuple[dict[str, Any] | None, ShellExecutionTicket | None]:
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
        raw_generation = pending.pop("_approval_generation", 0)
        if type(raw_generation) is not int:
            return pending, None
        claimed_generation = raw_generation
        current_generation = approvals.current_generation()
        try:
            stop_set = bool(run_registry.stop_flag.is_set())
        except Exception:
            return pending, None
        if stop_set or claimed_generation != int(current_generation or 0):
            return pending, None
        try:
            cwd = safe_project_cwd(
                str(pending.get("project") or ""),
                str(pending.get("cwd") or "."),
            )
        except Exception:
            return pending, None
        if type(timeout) is not int or type(output_limit) is not int:
            return pending, None
        ticket = ShellExecutionTicket(
            command=str(pending.get("command") or ""),
            cwd=cwd,
            generation=claimed_generation,
            timeout=timeout,
            output_limit=output_limit,
        )
        return pending, ticket


def claim_shell_ticket(
    ctx: TaskState,
    approval_id: str,
    *,
    timeout: int,
    output_limit: int,
) -> tuple[dict[str, Any] | None, ShellExecutionTicket | None]:
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


def execute_shell_ticket(ctx: TaskState, ticket: ShellExecutionTicket) -> dict[str, Any]:
    """Execute an already-claimed ticket. Final Stop check and Popen happen
    under the spawn gate; waiting happens outside the gate."""
    raw_command: object = ticket.command
    if not isinstance(raw_command, str):
        return {
            "ok": False,
            "status": "spawn_error",
            "error": "command required",
            "exit_code": None,
            "output": "",
        }
    command = raw_command.strip()
    if not command:
        return {
            "ok": False,
            "status": "spawn_error",
            "error": "command required",
            "exit_code": None,
            "output": "",
        }
    proc = None
    completed = None
    job = None
    capture_truncated = False
    try:
        # The gate is deliberately *not* ``ctx.lock``: the final check calls
        # ``ctx.approval_generation()``, which takes ``ctx.lock`` itself, so
        # gating on ``ctx.lock`` would self-deadlock.
        with ctx._shell_spawn_gate:
            if not _approval_generation_current(ctx, ticket.generation):
                return _stopped_shell_result()
            stop_flag = ctx.run_registry.stop_flag
            if bool(stop_flag.is_set()):
                return _stopped_shell_result()
            # Commit point: Popen while still holding the gate so a Stop that
            # needs the same gate to bump the generation cannot interleave.
            with cancellation.scope(stop_flag):
                proc, job = cancellation.start_process(
                    command,
                    cwd=ticket.cwd,
                    shell=True,
                )
        stop_flag_after = ctx.run_registry.stop_flag
        with cancellation.scope(stop_flag_after):
            from codey.runtime.core.output_capture import CAPTURE_LIMIT_BYTES

            completed = cancellation.wait_process(
                proc, job, command, ticket.timeout,
                capture_limit_bytes=CAPTURE_LIMIT_BYTES,
            )
            capture_truncated = bool(
                completed.stdout_truncated or completed.stderr_truncated
            )
    except (cancellation.TaskCancelled, cancellation.DeadlineExceeded):
        return _stopped_shell_result()
    except cancellation.ProcessOutputReadError as exc:
        return {
            "ok": False,
            "status": "output_read_error",
            "error": f"failed reading shell output ({exc})",
            "exit_code": None,
            "output": "",
            "truncated": True,
        }
    except cancellation.PipeDrainTimeout as exc:
        # wait_process() already terminated the tree and closed the Job.
        return {
            "ok": False,
            "status": "drain_timeout",
            "error": f"shell output pipe did not drain ({exc}); output is incomplete",
            "exit_code": None,
            "output": "",
            "truncated": True,
            "capture_truncated": True,
        }
    except subprocess.TimeoutExpired:
        # wait_process() already terminated the tree and closed the Job.
        return {
            "ok": False,
            "status": "timeout",
            "error": f"command timed out after {ticket.timeout}s",
            "exit_code": None,
            "output": "",
        }
    except Exception as exc:
        # Start failures (never spawned) keep spawn_error; anything that
        # fails after the spawn gate committed is a wait failure instead.
        return {
            "ok": False,
            "status": "spawn_error" if proc is None else "wait_error",
            "error": str(exc),
            "exit_code": None,
            "output": "",
        }

    output_parts: list[str] = []
    if completed is not None and completed.stdout:
        output_parts.append(completed.stdout.rstrip())
    if completed is not None and completed.stderr:
        output_parts.append("[stderr]\n" + completed.stderr.rstrip())
    output = "\n\n".join(output_parts) or "(no output)"
    output, display_truncated = clip_middle(output, ticket.output_limit)
    truncated = bool(display_truncated or capture_truncated)
    result: dict[str, Any] = {
        "ok": True,
        "status": "exit",
        "error": None,
        "exit_code": completed.returncode if completed is not None else None,
        "output": output,
        "truncated": truncated,
    }
    if capture_truncated:
        result["capture_truncated"] = True
    return result


def execute_approved_shell(
    ctx: TaskState,
    project: str | Path,
    rel: str,
    command: object,
    *,
    timeout: int | None = None,
    output_limit: int | None = None,
    expected_approval_generation: int | None = None,
) -> dict[str, Any]:
    """Direct-execution entry (tests, headless). Approval-card flow must use
    ``shell_service.claim_shell_ticket`` + ``execute_shell_ticket`` instead."""
    if not isinstance(command, str):
        return {
            "ok": False,
            "status": "spawn_error",
            "error": "command required",
            "exit_code": None,
            "output": "",
        }
    command = command.strip()
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
    result: dict[str, Any],
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
    decision = (
        "The user denied this shell command; it was not executed:\n"
        if result.get("status") == "denied"
        else "The user approved and ran this shell command:\n"
    )
    denial_note = (
        "Do not retry the denied command without new user authorization.\n\n"
        if result.get("status") == "denied"
        else ""
    )
    return (
        "Continue the interrupted task in this same conversation.\n"
        f"{decision}"
        f"{command}\n\n"
        f"Exit code: {result.get('exit_code')}\n"
        "Output:\n"
        f"{result.get('output') or result.get('error') or '(no output)'}\n\n"
        f"{truncation_note}"
        f"{denial_note}"
        f"{setup_block}"
        f"{checklist_block}"
        f"{followup_block}"
        f"{deferred_block}"
        "Use this result to continue the original task. If the task is complete,"
        " reply with a JSON done tool call."
    )


def build_shell_approval_continuation_plan(
    *,
    pending: dict[str, Any],
    result: dict[str, Any],
    active_run: RunSnapshot | None = None,
    shell_results: tuple[dict[str, object], ...] = (),
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
        shell_results=tuple(shell_results or ()),
    )


def shell_continuation_setup_context(pending: dict[str, Any]) -> str:
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


def shell_followup_verification_candidates(project: str | Path, risk_label: object) -> Any:
    if risk_label not in {"dependency_install", "dev_server", "publish"}:
        return ()
    return safe_verification_candidates(project)


def shell_followup_hints(
    *,
    pending: dict[str, Any],
    result: dict[str, Any],
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
    "shell_result_content",
    "shell_result_payload",
    "shell_result_row",
]
