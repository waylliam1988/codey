"""Headless JSONL entry point backed by the production task entry.

This module does not own an agent loop.  It adapts the task entry to a bounded
machine-readable stream so CLI/CI callers can use the same local execution
spine as the UI.
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from codey.agents.request import DEFAULT_MAX_TURNS
from codey.agents.runner import run as default_agent_run
from codey.workspace.changes import collect_changes as default_collect_changes
from codey.runtime.events import MAX_EVENT_RESULT_CHARS, MAX_EVENT_TEXT_CHARS, clip_event_text
from codey.storage.local_store import DEFAULT_STATE_HOME
from codey.providers.diagnostics import capture_provider_failure as default_capture_provider_failure
from codey.providers import (
    DEFAULT_PROVIDER_ID,
    connect_fresh_provider_tab as default_connect_fresh_provider_tab,
    connect_provider as default_connect_provider,
)
from codey.app.server import (
    AppContext,
    REVIEW_FIX_TURNS,
    REVIEW_LOG_LINES,
    _should_wait_for_local_ghost_sleep,
    is_git_repository,
)
from codey.task.model import TaskSubmission
from codey.operations.task_entry import TaskRunDeps, run_task_submission


SCHEMA_VERSION = 1
HEADLESS_SESSION_PREFIX = "headless_"


@dataclass(frozen=True)
class HeadlessRequest:
    project: Path
    task: str
    provider_id: str = DEFAULT_PROVIDER_ID
    max_turns: int = DEFAULT_MAX_TURNS
    session_id: str = ""
    run_id: str = ""
    intent: str = "project"
    state_home: Path | None = DEFAULT_STATE_HOME
    port: int = 9222


@dataclass(frozen=True)
class HeadlessResult:
    exit_code: int
    run_id: str
    session_id: str
    stop_reason: str
    ledger_path: str = ""


class HeadlessAppContext(AppContext):
    def __init__(
        self,
        state_home: str | Path | None,
        *,
        port: int,
        emit_jsonl: Callable[[dict[str, object]], None],
        connect_provider: Callable[..., Any] = default_connect_provider,
    ) -> None:
        super().__init__(state_home)
        self.port = int(port)
        self._emit_jsonl = emit_jsonl
        self._connect_provider = connect_provider
        self.shell_rejected = False
        self._run_modes: dict[str, str] = {}

    def get_provider(self, provider_id: str = DEFAULT_PROVIDER_ID):
        self.set_run_status("connecting")
        self.emit({"type": "status", "status": "connecting"})
        provider = self._connect_provider(provider_id, port=self.port)
        self.set_run_status("running")
        return provider

    def emit(self, event: dict) -> None:
        payload_event = self._event_with_headless_fields(event)
        super().emit(payload_event)
        payload = headless_event_payload(payload_event)
        if payload is not None:
            self._emit_jsonl(payload)
        if payload_event.get("type") == "shell_request":
            self.shell_rejected = True
            self.run_registry.stop_flag.set()
            rejected = {
                "schema_version": SCHEMA_VERSION,
                "type": "shell_rejected",
                "run_id": str(payload_event.get("run_id") or ""),
                "session_id": str(payload_event.get("session_id") or ""),
                "reason": "headless_default_deny",
                "command": clip_event_text(payload_event.get("command") or ""),
                "cwd": clip_event_text(payload_event.get("cwd") or ".", 240),
            }
            self._emit_jsonl(rejected)

    def _event_with_headless_fields(self, event: dict) -> dict:
        payload = dict(event)
        event_type = str(payload.get("type") or "")
        run_id = str(payload.get("run_id") or "")
        if event_type == "task_start" and run_id:
            mode = str(payload.get("mode") or "").strip()
            if mode:
                self._run_modes[run_id] = mode
            return payload
        if event_type != "task_done":
            return payload
        if run_id and not str(payload.get("mode") or "").strip():
            mode = self._run_modes.get(run_id, "")
            if mode:
                payload["mode"] = mode
        if payload.get("ledger_path"):
            return payload
        if self.run_ledgers is None:
            return payload
        session_id = str(payload.get("session_id") or "")
        if not run_id or not session_id:
            return payload
        try:
            path = self.run_ledgers.path_for(session_id, run_id)
            if path.exists():
                payload["ledger_path"] = str(path)
        except Exception:
            pass
        return payload


def emit_jsonl(payload: dict[str, object], *, file=sys.stdout) -> None:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoding = getattr(file, "encoding", None) or "utf-8"
    safe = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    file.write(safe + "\n")


def run_headless(
    request: HeadlessRequest,
    *,
    emit_jsonl: Callable[[dict[str, object]], None],
    agent_run: Callable | None = None,
    collect_changes: Callable | None = None,
    capture_provider_failure: Callable | None = None,
    connect_provider: Callable[..., Any] = default_connect_provider,
    connect_fresh_provider: Callable[..., Any] = default_connect_fresh_provider_tab,
) -> HeadlessResult:
    project = Path(request.project).expanduser().resolve()
    project.mkdir(parents=True, exist_ok=True)
    session_id = request.session_id or HEADLESS_SESSION_PREFIX + uuid.uuid4().hex[:12]
    state_home = Path(request.state_home).expanduser() if request.state_home else None
    state = HeadlessAppContext(
        state_home,
        port=request.port,
        emit_jsonl=emit_jsonl,
        connect_provider=connect_provider,
    )
    pre_reserved_run_id = _pre_reserve_run_id(
        state,
        request=request,
        session_id=session_id,
        project=project,
    )
    deps = TaskRunDeps(
        state=state,
        agent_run=agent_run or default_agent_run,
        collect_changes=collect_changes or default_collect_changes,
        run_review=_no_headless_review,
        capture_provider_failure=capture_provider_failure or default_capture_provider_failure,
        project_facts=state.project_facts,
        work_checkpoints=state.work_checkpoints,
        workspace_revisions=state.workspace_revisions,
        run_ledgers=state.run_ledgers,
        run_traces=state.run_traces,
        evidence_ledgers=state.evidence_ledgers,
        managed_outputs=state.managed_outputs,
        knowledge_store=state.knowledge_store,
        is_git_repository=is_git_repository,
        review_fix_turns=REVIEW_FIX_TURNS,
        review_log_lines=REVIEW_LOG_LINES,
        ghost_router_provider_factory=(
            (lambda provider_id: connect_fresh_provider(provider_id, port=request.port))
            if _request_intent(request.intent) == "auto"
            else None
        ),
        runtime_effects=state.runtime_effects,
    )
    try:
        run_task_submission(
            deps,
            TaskSubmission(
                session_id=session_id,
                project=str(project),
                task=request.task,
                max_turns=request.max_turns,
                continue_task=False,
                provider_id=request.provider_id,
                intent=_request_intent(request.intent),
                run_id=pre_reserved_run_id,
            ),
        )
    finally:
        if _should_wait_for_local_ghost_sleep(state.state_home):
            state.wait_for_ghost_sleep()
    terminal = dict(state.run_registry.last_terminal_event() or {})
    run_id = str(terminal.get("run_id") or request.run_id or "")
    stop_reason = str(terminal.get("stop_reason") or "error")
    ledger_path = ""
    if state.run_ledgers is not None and run_id:
        try:
            path = state.run_ledgers.path_for(session_id, run_id)
            if path.exists():
                ledger_path = str(path)
        except Exception:
            ledger_path = ""
    exit_code = 0 if stop_reason == "done" and not state.shell_rejected else 1
    return HeadlessResult(
        exit_code=exit_code,
        run_id=run_id,
        session_id=session_id,
        stop_reason=stop_reason,
        ledger_path=ledger_path,
    )


def headless_event_payload(event: dict) -> dict[str, object] | None:
    if not isinstance(event, dict):
        return None
    event_type = str(event.get("type") or "")
    common = {
        "schema_version": SCHEMA_VERSION,
        "type": event_type,
    }
    for key in ("run_id", "session_id"):
        value = str(event.get(key) or "")
        if value:
            common[key] = value
    if event_type == "task_start":
        return {
            **common,
            "project": clip_event_text(event.get("project") or "", 500),
            "provider": clip_event_text(event.get("provider") or "", 80),
            "mode": clip_event_text(event.get("mode") or "", 40),
            "max_turns": _int_or_zero(event.get("max_turns")),
        }
    if event_type == "status":
        return {
            **common,
            "status": clip_event_text(event.get("status") or event.get("text") or "", 80),
        }
    if event_type == "info":
        return {**common, "text": clip_event_text(event.get("text") or "")}
    if event_type == "turn":
        payload = {**common, "turn": _int_or_zero(event.get("turn"))}
        if event.get("note"):
            payload["note"] = clip_event_text(event.get("note") or "")
        return payload
    if event_type == "tool_started":
        payload = {
            **common,
            "turn": _int_or_zero(event.get("turn")),
            "tool_id": clip_event_text(event.get("tool_id") or "", 40),
            "tool": clip_event_text(event.get("kind") or "", 80),
            "path": clip_event_text(event.get("path") or "", 240),
            "activity": clip_event_text(event.get("activity") or ""),
        }
        command = clip_event_text(event.get("command") or "")
        if command:
            payload["command"] = command
        return payload
    if event_type == "tool":
        payload = {
            **common,
            "turn": _int_or_zero(event.get("turn")),
            "tool_id": clip_event_text(event.get("tool_id") or "", 40),
            "tool": clip_event_text(event.get("kind") or "", 80),
            "path": clip_event_text(event.get("path") or "", 240),
            "ok": bool(event.get("ok", not bool(event.get("error")))),
            "status": clip_event_text(event.get("status") or "", 80),
            "changed": bool(event.get("changed", False)),
            "truncated": bool(event.get("truncated", False)),
            "result": clip_event_text(event.get("result") or "", MAX_EVENT_RESULT_CHARS),
        }
        command = clip_event_text(event.get("command") or "")
        if command:
            payload["command"] = command
        if event.get("exit_code") is not None:
            payload["exit_code"] = _int_or_zero(event.get("exit_code"))
        _copy_if_present(payload, event, "output_handle", limit=120)
        for key in ("output_bytes", "output_stored_bytes"):
            if event.get(key) is not None:
                payload[key] = _int_or_zero(event.get(key))
        _copy_if_present(payload, event, "output_sha256", limit=80)
        return payload
    if event_type == "task_done":
        payload = {
            **common,
            "summary": clip_event_text(event.get("summary") or ""),
            "stop_reason": clip_event_text(event.get("stop_reason") or "", 80),
            "turns": _int_or_zero(event.get("turns")),
            "max_turns": _int_or_zero(event.get("max_turns")),
            "provider": clip_event_text(event.get("provider") or "", 80),
            "mode": clip_event_text(event.get("mode") or "", 40),
        }
        if event.get("changed") is not None:
            payload["changed"] = bool(event.get("changed"))
        _copy_if_present(payload, event, "ledger_path", limit=500)
        receipt = event.get("receipt")
        if isinstance(receipt, dict):
            payload["receipt"] = _bounded_receipt(receipt)
        changes = event.get("changes")
        if isinstance(changes, dict):
            payload["changes"] = _bounded_changes(changes)
        failure = event.get("provider_failure")
        if isinstance(failure, dict):
            payload["provider_failure"] = _bounded_provider_failure(failure)
        return payload
    return None


def _no_headless_review(**_kwargs):
    return None


def _pre_reserve_run_id(
    state: HeadlessAppContext,
    *,
    request: HeadlessRequest,
    session_id: str,
    project: Path,
) -> str:
    requested = str(request.run_id or "").strip()
    if not requested:
        return ""
    reserved = state.reserve_run(
        session_id=session_id,
        project=str(project),
        task=request.task,
        provider_id=request.provider_id,
    )
    if reserved is None:
        return ""
    state.replace_reserved_run(reserved.run_id, replace(reserved, run_id=requested))
    return requested


def _request_intent(value: str) -> str:
    text = str(value or "project").strip().lower()
    if text in {"readonly", "planning", "planning_readonly"}:
        return "planning_readonly"
    if text in {"auto", "chat", "research", "project", "hybrid", "review"}:
        return text
    return "project"


def _int_or_zero(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _copy_if_present(
    target: dict[str, object],
    source: dict,
    key: str,
    *,
    limit: int = MAX_EVENT_TEXT_CHARS,
) -> None:
    value = str(source.get(key) or "")
    if value:
        target[key] = clip_event_text(value, limit)


def _bounded_receipt(receipt: dict) -> dict[str, object]:
    """Bounded schema-v1 receipt projection for the JSONL stream."""

    payload: dict[str, object] = {}
    schema = receipt.get("schema_version")
    if isinstance(schema, int):
        payload["schema_version"] = schema
    display = receipt.get("display")
    if isinstance(display, dict):
        section: dict[str, object] = {}
        summary = clip_event_text(display.get("summary") or "")
        if summary:
            section["summary"] = summary
        detail = clip_event_text(display.get("detail") or "")
        if detail:
            section["detail"] = detail
        if section:
            payload["display"] = section
    work = receipt.get("work")
    if isinstance(work, dict):
        section = {}
        if "changed_count" in work:
            section["changed_count"] = _int_or_zero(work.get("changed_count"))
        mode = clip_event_text(work.get("mode") or "", 40)
        if mode:
            section["mode"] = mode
        if "restore_available" in work:
            section["restore_available"] = bool(work.get("restore_available"))
        if section:
            payload["work"] = section
    verification = receipt.get("verification")
    if isinstance(verification, dict):
        section = {}
        trust = clip_event_text(verification.get("trust") or "", 20)
        if trust:
            section["trust"] = trust
        if "checks_passed" in verification:
            section["checks_passed"] = bool(verification.get("checks_passed"))
        state = clip_event_text(verification.get("state") or "", 40)
        if state:
            section["state"] = state
        proof_refs = [
            clip_event_text(ref, 120)
            for ref in (verification.get("proof_refs") or [])
        ]
        bounded_refs = [ref for ref in proof_refs if ref][:2]
        if bounded_refs:
            section["proof_refs"] = bounded_refs
        if section:
            payload["verification"] = section
    integrity = receipt.get("integrity")
    if isinstance(integrity, dict):
        section = {}
        status = clip_event_text(integrity.get("status") or "", 20)
        if status:
            section["status"] = status
        severity = clip_event_text(integrity.get("severity") or "", 20)
        if severity:
            section["severity"] = severity
        reason_codes = [
            clip_event_text(code, 80)
            for code in (integrity.get("reason_codes") or [])
        ]
        bounded_codes = [code for code in reason_codes if code]
        if bounded_codes:
            section["reason_codes"] = bounded_codes
        paths = [
            clip_event_text(path, 240)
            for path in (integrity.get("affected_paths") or [])
        ]
        bounded_paths = [path for path in paths if path][:4]
        if bounded_paths:
            section["affected_paths"] = bounded_paths
        refs = [
            clip_event_text(ref, 80)
            for ref in (integrity.get("refs") or [])
        ]
        bounded_integrity_refs = [ref for ref in refs if ref][:4]
        if bounded_integrity_refs:
            section["refs"] = bounded_integrity_refs
        if section:
            payload["integrity"] = section
    return payload


def _bounded_changes(changes: dict) -> dict[str, object]:
    files = []
    for item in list(changes.get("files") or [])[:3]:
        if not isinstance(item, dict):
            continue
        files.append({
            "path": clip_event_text(item.get("path") or "", 240),
            "status": clip_event_text(item.get("status") or "", 40),
        })
    return {
        "changed_count": _int_or_zero(changes.get("changed_count")),
        "mode": clip_event_text(changes.get("mode") or "", 40),
        "files": files,
    }


def _bounded_provider_failure(failure: dict) -> dict[str, object]:
    return {
        "kind": clip_event_text(failure.get("kind") or "", 80),
        "action": clip_event_text(failure.get("action") or "", 80),
        "message": clip_event_text(failure.get("message") or ""),
    }
