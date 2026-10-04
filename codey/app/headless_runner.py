"""Headless JSONL entry point backed by the production task entry.

This module does not own an agent loop.  It adapts the task entry to a bounded
machine-readable stream so CLI/CI callers can use the same local execution
spine as the UI.
"""

from __future__ import annotations

import contextlib
import json
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codey.agents.request import DEFAULT_MAX_TURNS
from codey.agents.shell_approval import shell_command_event_fields
from codey.app.context import (
    REVIEW_FIX_TURNS,
    REVIEW_LOG_LINES,
    AppContext,
)
from codey.app.event_payloads import SCHEMA_VERSION, machine_event_payload
from codey.operations.project_adapter import run as default_agent_run
from codey.operations.task_entry import run_task_submission
from codey.operations.task_run import TaskRunDeps
from codey.providers.catalog import DEFAULT_PROVIDER_ID
from codey.providers.diagnostics import capture_provider_failure as default_capture_provider_failure
from codey.providers.registry import connect_provider as default_connect_provider
from codey.runtime.observe.events import clip_event_text
from codey.storage.local_store import DEFAULT_STATE_HOME
from codey.task.model import TaskSubmission
from codey.workspace.changes import collect_changes as default_collect_changes
from codey.workspace.changes import is_git_repository

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
    requested_capabilities: tuple[str, ...] = ()
    strict_research: bool = False
    sources_open_required: bool = False
    project_changes_required: bool | None = None
    # Research runs may opt into an isolated vault outside the user's default
    # state home. The caller owns the path; headless still closes the store.
    research_store_root: Path | None = None
    review_source_run_id: str = ""


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
        super().__init__(
            state_home,
            replay_limit=0,
        )
        self.port = int(port)
        self._emit_jsonl = emit_jsonl
        self._connect_provider = connect_provider
        self.shell_rejected = False

    def get_provider(self, provider_id: str = DEFAULT_PROVIDER_ID):
        self.set_run_status("connecting")
        self.emit({"type": "status", "status": "connecting"})
        provider = self._connect_provider(provider_id, port=self.port)
        self.set_run_status("running")
        self.emit({"type": "status", "status": "running"})
        return provider

    def _on_event_emitted(self, payload_event: dict) -> None:
        payload = machine_event_payload(payload_event)
        if payload is not None:
            self._emit_jsonl(payload)
        if payload_event.get("type") == "shell_request":
            self.shell_rejected = True
            with contextlib.suppress(Exception):
                self.request_stop()
            command_fields = shell_command_event_fields(payload_event)
            rejected = {
                "schema_version": SCHEMA_VERSION,
                "type": "shell_rejected",
                "run_id": str(payload_event.get("run_id") or ""),
                "session_id": str(payload_event.get("session_id") or ""),
                "reason": "headless_default_deny",
                **command_fields,
                "cwd": clip_event_text(payload_event.get("cwd") or ".", 240),
            }
            self._emit_jsonl(rejected)



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
    connect_reviewer: Callable[..., Any] | None = None,
) -> HeadlessResult:
    # An explicit connector enables the existing project review phase for
    # embedded callers/gates. CLI defaults still add no Reviewer requests.
    if connect_reviewer is not None and _request_intent(request.intent) not in {"project", "review"}:
        raise ValueError("connect_reviewer requires project or review intent")
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
    task_result: HeadlessResult | None = None
    task_error: BaseException | None = None
    try:
        pre_reserved_run_id = _pre_reserve_run_id(
            state,
            request=request,
            session_id=session_id,
            project=project,
        )
        if str(request.run_id or "").strip() and not pre_reserved_run_id:
            reason = (
                "duplicate"
                if _headless_run_id_exists(
                    state,
                    session_id=session_id,
                    run_id=str(request.run_id or "").strip(),
                )
                else "busy"
            )
            emit_jsonl({
                "schema_version": SCHEMA_VERSION,
                "type": "task_done",
                "run_id": str(request.run_id or "").strip(),
                "session_id": session_id,
                "stop_reason": reason,
            })
            task_result = HeadlessResult(
                exit_code=1,
                run_id=str(request.run_id or "").strip(),
                session_id=session_id,
                stop_reason=reason,
                ledger_path="",
            )
            task_error = None
        else:
            task_result, task_error = _run_headless_task(
                state,
                request=request,
                session_id=session_id,
                project=project,
                pre_reserved_run_id=pre_reserved_run_id,
                agent_run=agent_run,
                collect_changes=collect_changes,
                capture_provider_failure=capture_provider_failure,
                connect_reviewer=connect_reviewer,
            )
    except BaseException as exc:
        task_result = None
        task_error = exc
    run_id_for_event = str(request.run_id or "").strip()
    if task_result is not None and str(task_result.run_id or "").strip():
        run_id_for_event = str(task_result.run_id)
    # Ghost owns its stores while alive: a False close or a close raising
    # retains resources instead of releasing files under the daemon. One
    # bounded second chance; a still-incomplete close fails the result
    # explicitly without masking the task's own exception.
    close_error: BaseException | None = None
    try:
        closed = state.close()
    except BaseException as exc:
        close_error = exc
        closed = False
    if not closed:
        with contextlib.suppress(Exception):
            state.wait_for_ghost_sleep(timeout=30)
        try:
            closed = state.close()
        except BaseException as exc:
            if close_error is None:
                close_error = exc
            closed = False
        else:
            if closed:
                close_error = None

    def _emit_close(run_id: str) -> None:
        # Run-level shutdown status, always bounded and never masking the
        # task's own error or exit code.
        with contextlib.suppress(Exception):
            emit_jsonl({
                "schema_version": SCHEMA_VERSION,
                "type": "headless_close",
                "run_id": run_id,
                "session_id": session_id,
                "stop_reason": "close_incomplete",
                "exit_code": 1,
            })

    if task_error is not None:
        if not closed:
            with contextlib.suppress(Exception):
                task_error.add_note(
                    "headless close incomplete: ghost still alive or resources retained"
                )
            if close_error is not None:
                with contextlib.suppress(Exception):
                    task_error.add_note(
                        f"headless close raised {type(close_error).__name__}: {close_error}"
                    )
            _emit_close(run_id_for_event)
        raise task_error
    assert task_result is not None
    if not closed:
        # task_done already recorded the task verdict; this extra run-level
        # event keeps retained resources visible even when the task itself
        # already failed. A green task becomes close_incomplete; a failed
        # task keeps its own exit as the primary result.
        _emit_close(run_id_for_event)
        if task_result.exit_code == 0:
            return HeadlessResult(
                exit_code=1,
                run_id=task_result.run_id,
                session_id=task_result.session_id,
                stop_reason="close_incomplete",
                ledger_path=task_result.ledger_path,
            )
        return task_result
    return task_result


def _run_headless_task(
    state: HeadlessAppContext,
    *,
    request: HeadlessRequest,
    session_id: str,
    project: Path,
    pre_reserved_run_id: str,
    agent_run: Callable | None,
    collect_changes: Callable | None,
    capture_provider_failure: Callable | None,
    connect_reviewer: Callable[..., Any] | None,
) -> tuple[HeadlessResult | None, BaseException | None]:
    """Run the submission; the caller owns shutdown so close never masks this."""
    try:
        if (
            _request_intent(request.intent) == "research"
            and request.research_store_root is not None
            and state.knowledge_store is None
        ):
            from codey.knowledge.store import KnowledgeStore

            state.knowledge_store = KnowledgeStore(request.research_store_root)
        deps = TaskRunDeps.from_submission_stores(
            state=state,
            stores=state.task_submission_stores,
            agent_run=agent_run or default_agent_run,
            collect_changes=collect_changes or default_collect_changes,
            run_review=_headless_review_for(request, state, connect_reviewer),
            capture_provider_failure=capture_provider_failure or default_capture_provider_failure,
            is_git_repository=is_git_repository,
            review_fix_turns=REVIEW_FIX_TURNS,
            review_log_lines=REVIEW_LOG_LINES,
        )
        entry_requested = tuple(getattr(request, "requested_capabilities", ()) or ())
        entry_strict = bool(getattr(request, "strict_research", False))
        if not entry_strict and _request_intent(request.intent) == "research":
            entry_strict = True
        try:
            explicit = getattr(request, "project_changes_required", None)
            if explicit is True:
                entry_requires = True
            elif explicit is False:
                entry_requires = False
            else:
                # Headless execution is an explicit machine entry. It only
                # requires a project change when the caller opts in; an empty
                # project may validly complete an inspection or planning task.
                entry_requires = False
        except Exception:
            entry_requires = False
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
                requested_capabilities=entry_requested,
                strict_research=entry_strict,
                sources_open_required=bool(getattr(request, "sources_open_required", False)),
                project_changes_required=entry_requires,
                review_source_run_id=request.review_source_run_id,
            ),
        )
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
        ), None
    except BaseException as exc:
        return None, exc


def _no_headless_review(**_kwargs):
    return None


def _headless_review_for(
    request: HeadlessRequest,
    state: HeadlessAppContext,
    connect_reviewer: Callable[..., Any] | None = None,
):
    if _request_intent(request.intent) != "review" and connect_reviewer is None:
        return _no_headless_review
    from codey.app.review_service import run_review

    def _run_review(**kwargs):
        return run_review(state, connect_reviewer=connect_reviewer or state.get_provider, **kwargs)

    return _run_review


def _headless_run_id_exists(
    state: HeadlessAppContext,
    *,
    session_id: str,
    run_id: str,
) -> bool:
    """True when a durable operation or ledger already owns this run_id."""
    try:
        ledgers = getattr(state, "run_ledgers", None)
        if ledgers is not None:
            try:
                if ledgers.path_for(session_id, run_id).exists():
                    return True
            except Exception:
                pass
        runtime_log = getattr(state, "runtime_log", None)
        if runtime_log is not None:
            from codey.runtime.core.operation_state import operation_id_for_run

            try:
                projection = runtime_log.projection(session_id)
            except Exception:
                return False
            operations = getattr(projection, "operations", {}) or {}
            return operation_id_for_run(run_id) in operations
    except Exception:
        return False
    return False


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
    # Never recycle a durable run_id into a fresh operation: same run_id maps
    # to the same operation/lane, so reuse would append to чужой ledger and
    # replay settled effects.
    if _headless_run_id_exists(state, session_id=session_id, run_id=requested):
        return ""
    reserved = state.reserve_run(
        session_id=session_id,
        project=str(project),
        task=request.task,
        provider_id=request.provider_id,
        run_id=requested,
    )
    return requested if reserved is not None else ""


def _request_intent(value: str) -> str:
    text = str(value or "project").strip().lower()
    if text in {"readonly", "planning", "planning_readonly"}:
        return "planning_readonly"
    if text in {"auto", "chat", "research", "project", "hybrid", "review"}:
        return text
    return "project"
