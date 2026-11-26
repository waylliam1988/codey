"""Policy-controlled command execution and retained output projection."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from codey.policies.action import DECISION_DENY, ActionPolicyDecision, ActionSubject, evaluate_action
from codey.policies.run_command_semantics import RunCommandPolicyError, canonical_run_command, is_suite_run_command
from codey.runtime.core import cancellation
from codey.runtime.core.execution_receipt import ExecutionOutputReference, ExecutionReceipt
from codey.runtime.core.output_capture import CAPTURE_LIMIT_BYTES
from codey.runtime.core.process_io import ProcessIO, ProcessOptions
from codey.storage.execution_output_store import ExecutionOutputStore
from codey.toolchain.outcome import ToolOutcome, _first_model_line
from codey.utils.failure_output import failure_excerpt
from codey.utils.text_budget import clip_middle, prune_dependency_stack_frames

RUN_TIMEOUT_SECONDS = 90
RUN_SUITE_TIMEOUT_SECONDS = 300
RUN_OUTPUT_LIMIT = 24_000


def _policy_error_outcome(decision: ActionPolicyDecision) -> ToolOutcome:
    message = decision.display or "action denied by policy"
    text = message if message.startswith("ERROR:") else f"ERROR: {message}"
    return ToolOutcome(
        text,
        False,
        presentation={"status": "error", "result": _first_model_line(text, 200)},
        audit={"error_code": "policy_denied", "policy_decision": decision.to_audit_payload()},
        error_code="policy_denied",
    )


@dataclass(frozen=True)
class RunCommandRawResult:
    command: str
    output: str
    ok: bool
    exit_code: int
    started_at: str
    finished_at: str
    duration_ms: int | None
    stdout_bytes: int
    stderr_bytes: int
    capture_truncated: bool
    execution_id: str = ""
    full_output_retained: bool = False
    output_ref: ExecutionOutputReference | None = None


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _is_suite_run_command(argv: list[str]) -> bool:
    """Recognize verification suites that legitimately need a longer budget."""
    return is_suite_run_command(argv)


def _process_argv(argv: list[str]) -> list[str]:
    """Resolve a bare Windows executable before direct process launch.

    ``shutil.which`` and the Windows command shell both recognize ``.cmd``
    shims such as ``npm``, while ``CreateProcess`` used by ``shell=False``
    does not apply that extension search consistently. Keep the canonical
    user command unchanged for policy and receipts, but launch the resolved
    executable when one is available. Paths and explicitly suffixed names are
    left untouched.
    """
    if os.name != "nt" or not argv:
        return argv
    executable = str(argv[0] or "")
    if not executable or "/" in executable or "\\" in executable or Path(executable).suffix:
        return argv
    resolved = shutil.which(executable)
    if resolved is None:
        return argv
    return [str(resolved), *argv[1:]]


def run_command_raw(
    root: Path,
    rel: str,
    command: str,
    *,
    permission_profile: str,
    phase: str = "tool_runtime",
    capture_limit_bytes: int = CAPTURE_LIMIT_BYTES,
    execution_store: ExecutionOutputStore | None = None,
    session_id: str = "",
    run_id: str = "",
    execution_id: str = "",
    process_options: ProcessOptions | None = None,
    on_process_started: Callable[[ProcessIO], Callable[[], None]] | None = None,
) -> RunCommandRawResult | ToolOutcome:
    command = command.strip()
    decision = evaluate_action(ActionSubject(
        kind="run_command",
        phase=phase,
        permission_profile=permission_profile,
        project=str(root),
        path=rel or ".",
        command=command,
        tool_name="run",
    ))
    if decision.decision == DECISION_DENY:
        return _policy_error_outcome(decision)
    try:
        canonical = canonical_run_command(root, rel or ".", command)
    except RunCommandPolicyError as exc:
        # Fail closed: an untokenizable command never executes.
        return ToolOutcome.error(exc.display, error_code=exc.reason_code)
    argv = list(canonical.argv)
    process_argv = _process_argv(argv)
    cwd = canonical.cwd
    if not cwd.is_dir():
        return ToolOutcome.error(f"not a directory: {rel}")
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    timeout = RUN_SUITE_TIMEOUT_SECONDS if _is_suite_run_command(argv) else RUN_TIMEOUT_SECONDS
    started_at = _utc_now_iso()
    started_monotonic = time.monotonic()
    observer = None
    if execution_store is not None:
        import uuid

        from codey.storage.execution_output_store import ExecutionOutputObserver
        execution_id = execution_id or "execution_" + uuid.uuid4().hex
        execution_store.create(execution_id, session_id=session_id or "run", run_id=run_id or "run", max_bytes=capture_limit_bytes)
        observer = ExecutionOutputObserver(execution_store, execution_id)
    def mark_unavailable() -> None:
        if execution_store is not None and execution_id:
            with contextlib.suppress(Exception):
                execution_store.unavailable(execution_id)
    output_ref: ExecutionOutputReference | None = None

    def mark_settled(code: int | None) -> bool:
        nonlocal output_ref
        if execution_store is not None and execution_id:
            with contextlib.suppress(Exception):
                snapshot = execution_store.settled(execution_id, code)
                output_ref = ExecutionOutputReference.from_snapshot(snapshot)
                return (snapshot["execution_state"] == "settled" and not snapshot["stdout"]["truncated"]
                        and not snapshot["stderr"]["truncated"])
        return False

    def failed_execution(status: str, message: str) -> ToolOutcome:
        mark_unavailable()
        receipt = ExecutionReceipt(status, None, "", truncated=True, capture_truncated=True,
                                   execution_id=execution_id, exit_code_scope="process")
        text = f"ERROR: {message}"
        return ToolOutcome(text + "\n\n" + receipt.render(), False, error_code=status, truncated=True,
            canonical={"execution": receipt.facts()},
            presentation={"status": "error", "result": _first_model_line(text, 200)},
            audit={**receipt.audit_facts(), "command_started_at": started_at,
                   "command_finished_at": _utc_now_iso(),
                   "command_duration_ms": max(0, int((time.monotonic() - started_monotonic) * 1000))})

    try:
        proc = cancellation.run_process(
            process_argv,
            cwd=cwd,
            env=env,
            timeout=timeout,
            shell=False,
            capture_limit_bytes=capture_limit_bytes,
            observer=observer,
            options=process_options,
            on_started=on_process_started,
        )
    except FileNotFoundError:
        mark_unavailable()
        return ToolOutcome.error(f"command not found: {argv[0]}", audit={"execution_id": execution_id} if execution_id else {})
    except cancellation.ProcessOutputReadError as exc:
        return failed_execution("output_read_error",
            f"failed reading command output ({exc}): {command}. "
            "Re-run a narrower command.",
        )
    except cancellation.PipeDrainTimeout as exc:
        return failed_execution("output_drain_timeout",
            f"command output pipe did not drain ({exc}); output is incomplete, "
            f"not a success: {command}. Re-run a narrower command.",
        )
    except subprocess.TimeoutExpired:
        message = (
            f"command timed out after {timeout}s (this is a timeout, not a test "
            f"failure): {command}. Re-run a smaller subset or a single test/file to "
            "verify instead of guessing a fix."
        )
        return failed_execution("timeout", message)
    duration_ms = max(0, int((time.monotonic() - started_monotonic) * 1000))

    output_parts = []
    if proc.stdout:
        output_parts.append(proc.stdout.rstrip())
    if proc.stderr:
        output_parts.append("[stderr]\n" + proc.stderr.rstrip())
    output = "\n\n".join(output_parts) or "(no output)"
    full_output_retained = mark_settled(proc.returncode)
    result = RunCommandRawResult(
        command=command,
        output=output,
        ok=proc.returncode == 0,
        exit_code=proc.returncode,
        started_at=started_at,
        finished_at=_utc_now_iso(),
        duration_ms=duration_ms,
        stdout_bytes=proc.stdout_bytes,
        stderr_bytes=proc.stderr_bytes,
        capture_truncated=(proc.stdout_truncated or proc.stderr_truncated),
        execution_id=execution_id,
        full_output_retained=full_output_retained,
        output_ref=output_ref,
    )
    return result


def project_run_command_result(root: Path, raw: RunCommandRawResult) -> ToolOutcome:
    output = prune_dependency_stack_frames(raw.output, root)
    output, display_truncated = clip_middle(output, RUN_OUTPUT_LIMIT)
    capture_truncated = raw.capture_truncated
    truncated = (capture_truncated or display_truncated)
    if capture_truncated:
        output += (
            "\nOnly head and tail of the process output were retained; "
            "run a narrower command to inspect omitted content."
        )
    receipt = ExecutionReceipt("exit", raw.exit_code, output, truncated=truncated,
        capture_truncated=capture_truncated, execution_id=raw.execution_id,
        exit_code_scope="process", full_output_retained=raw.full_output_retained, output_ref=raw.output_ref)
    display = (f"exit {raw.exit_code}: {raw.command}\nExit code scope: process\n"
               f"Full execution output recoverable: {str(receipt.full_output_recoverable).lower()}\n"
               + (failure_excerpt(raw.output) if raw.exit_code != 0 else "") + output)
    audit: dict[str, object] = dict(receipt.audit_facts())
    if raw.started_at:
        audit["command_started_at"] = raw.started_at
    if raw.finished_at:
        audit["command_finished_at"] = raw.finished_at
    if raw.duration_ms is not None:
        audit["command_duration_ms"] = max(0, min(raw.duration_ms, 10**9))
    stdout_bytes = max(0, raw.stdout_bytes)
    stderr_bytes = max(0, raw.stderr_bytes)
    audit["process_stdout_bytes"] = stdout_bytes
    audit["process_stderr_bytes"] = stderr_bytes
    audit["process_bytes"] = stdout_bytes + stderr_bytes
    return ToolOutcome(
        display,
        raw.ok,
        canonical={"execution": receipt.facts()},
        audit=audit,
        exit_code=raw.exit_code,
        truncated=truncated,
    )


def run_command(
    root: Path,
    rel: str,
    command: str,
    *,
    permission_profile: str,
    phase: str = "tool_runtime",
) -> ToolOutcome:
    raw = run_command_raw(
        root,
        rel,
        command,
        permission_profile=permission_profile,
        phase=phase,
    )
    if isinstance(raw, ToolOutcome):
        return raw
    return project_run_command_result(root, raw)
