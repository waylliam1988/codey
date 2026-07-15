"""Tiny HTTP + SSE server that drives the agent from a native UI.

Requires pywebview in addition to the standard library plus Playwright
(already used).

Endpoints
    GET  /                serves codey/web/index.html
    GET  /api/state       returns current run state as JSON
    GET  /api/ui_state    returns durable sidebar/chat UI state
    POST /api/ui_state    stores durable sidebar/chat UI state
    POST /api/run         body {project, task, provider, max_turns} → starts agent in
                          a background thread, returns {ok:true, run_id}
    GET  /api/changes     query {project} → returns git status + diff
    POST /api/changes     body {project} → returns git status + diff
    POST /api/shell_approval body {id, approved} → approve/reject shell request
    POST /api/stop        request cooperative stop of the current task
    GET  /api/events      Server-Sent Events stream of log lines

A single Codey instance can run one task at a time; while a task is running
new /api/run calls return 409.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from codey import cancellation, profile_doctor, provider_controls, provider_flow
from codey import __version__
from codey.agent import DEFAULT_MAX_TURNS, run as agent_run
from codey.browser_worker import submit as submit_browser_task
from codey.changes import (
    ChangeTracker,
    SnapshotStore,
    collect_changes,
    is_git_repository,
    restore_snapshot_changes,
)
from codey.conversation_store import ConversationStore
from codey.consensus import ConsensusAdvice, ConsensusResult, run_consensus, run_project_audit
from codey.handoff import ConversationContext
from codey.local_store import DEFAULT_STATE_HOME
from codey.providers import (
    DEFAULT_PROVIDER_ID,
    PROVIDER_LABELS,
    borrow_open_provider,
    connect_existing_provider,
    connect_provider,
    provider_tab_availability,
)
from codey.provider_diagnostics import ProviderFailure, capture_provider_failure
from codey.provider_supervisor import ProviderSupervisor
from codey.adapter_repair import AdapterRepairResult
from codey.self_repair import SelfRepairJob, SelfRepairSupervisor
from codey.self_repair_worker import run_self_repair_worker
from codey.project_facts import ProjectFactsStore
from codey.work_checkpoint import WorkCheckpointStore
from codey.review import (
    ReviewResult,
    parse_review_with_repair,
    render_review_prompt,
)
from codey.task_runner import TaskRequest, TaskRunner
from codey.text_budget import clip_middle
from codey.ui_state_store import UiStateStore

WEB_DIR = Path(__file__).parent / "web"
FOLDER_DIALOG_LOCK = threading.Lock()
SHELL_TIMEOUT = 120
SHELL_OUTPUT_LIMIT = 24_000
REVIEW_TIMEOUT = 300.0
REVIEW_FIX_TURNS = 12
REVIEW_LOG_LINES = 80
CONTROL_TEACH_TIMEOUT = 300.0
PROFILE_DOCTOR_TIMEOUT = 90.0
MAX_CONVERSATION_STATES = 32
def reviewer_candidates(writer_id: str) -> tuple[str, ...]:
    writer = (writer_id or DEFAULT_PROVIDER_ID).strip().lower()
    supervisor = getattr(globals().get("STATE"), "provider_supervisor", None)
    return tuple(
        provider_id
        for provider_id in PROVIDER_LABELS
        if provider_id != writer
        and (supervisor is None or supervisor.is_available(provider_id))
    )


def review_label(provider_id: str) -> str:
    return PROVIDER_LABELS.get(provider_id, provider_id)


def provider_availability() -> dict[str, bool]:
    statuses = provider_tab_availability()
    supervisor = getattr(globals().get("STATE"), "provider_supervisor", None)
    if supervisor is None:
        return statuses
    return {
        provider_id: available and supervisor.is_available(provider_id)
        for provider_id, available in statuses.items()
    }


def provider_payload(statuses: dict[str, bool] | None = None) -> list[dict]:
    statuses = statuses or {}
    return [
        {"id": provider_id, "label": label, "available": bool(statuses.get(provider_id))}
        for provider_id, label in PROVIDER_LABELS.items()
    ]


def provider_status_update(provider_id: str, available: bool) -> list[dict]:
    return [{
        "id": provider_id,
        "label": PROVIDER_LABELS.get(provider_id, provider_id),
        "available": available,
    }]


def _emit_review(session_id: str, text: str) -> None:
    STATE.emit({"type": "review", "session_id": session_id, "text": text})


def _run_review(
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
    execution_evidence: str = "",
) -> tuple[str, ReviewResult] | None:
    cancellation.check()
    last_error: Exception | None = None
    for reviewer_id in reviewer_candidates(writer_id):
        cancellation.check()
        reviewer = None
        try:
            reviewer = connect_existing_provider(reviewer_id)
            STATE.set_provider_session(reviewer_id, None)
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
                execution_evidence=execution_evidence,
            )
            with provider_controls.suppress_assistance():
                reply = reviewer.send(prompt, timeout=REVIEW_TIMEOUT)
                review = parse_review_with_repair(
                    reply,
                    lambda repair: reviewer.send(repair, timeout=REVIEW_TIMEOUT),
                )
            label = review_label(reviewer_id)
            if review.approved:
                _emit_review(session_id, f"{label} approved")
            else:
                _emit_review(session_id, f"{label} suggested changes")
            return reviewer_id, review
        except cancellation.TaskCancelled:
            raise
        except Exception as exc:
            last_error = exc
        finally:
            if reviewer is not None:
                try:
                    reviewer.close()
                except Exception:
                    pass
    if last_error is not None:
        raise last_error
    raise RuntimeError("no review model available")


def _connect_consensus_provider(selected_provider, provider_id: str):
    """Use an already-open sibling tab while a Writer provider is active."""

    owner_page = getattr(getattr(selected_provider, "session", None), "page", None)
    if owner_page is not None:
        helper = borrow_open_provider(provider_id, owner_page)
        if helper is None:
            raise RuntimeError(f"{review_label(provider_id)} tab is not open in this browser context")
        return helper
    return connect_existing_provider(provider_id)


def _run_consensus(
    *,
    selected_provider,
    selected_provider_id: str,
    task: str,
    context: str = "",
    draft: str = "",
    plan: bool = False,
    draft_first: bool = False,
    owner_prompt: str = "",
) -> ConsensusResult | None:
    return run_consensus(
        selected_provider=selected_provider,
        selected_provider_id=selected_provider_id,
        task=task,
        provider_ids=tuple(PROVIDER_LABELS),
        provider_labels=PROVIDER_LABELS,
        availability=provider_availability,
        connect_existing=lambda provider_id: _connect_consensus_provider(
            selected_provider,
            provider_id,
        ),
        clear_provider_session=lambda provider_id: STATE.set_provider_session(provider_id, None),
        context=context,
        draft=draft,
        plan=plan,
        draft_first=draft_first,
        owner_prompt=owner_prompt,
    )


def _run_project_audit(
    *,
    project: str | Path,
    selected_provider=None,
    selected_provider_id: str,
    task: str,
    context: str = "",
) -> tuple[ConsensusAdvice, ...]:
    return run_project_audit(
        project=project,
        selected_provider_id=selected_provider_id,
        task=task,
        provider_ids=tuple(PROVIDER_LABELS),
        provider_labels=PROVIDER_LABELS,
        availability=provider_availability,
        connect_existing=lambda provider_id: _connect_consensus_provider(
            selected_provider,
            provider_id,
        ),
        clear_provider_session=lambda provider_id: STATE.set_provider_session(provider_id, None),
        context=context,
    )


def _safe_project_cwd(project: str | Path, rel: str) -> Path:
    root = Path(project).expanduser().resolve()
    cwd = (root / (rel or ".")).resolve()
    if root not in cwd.parents and cwd != root:
        raise ValueError("cwd escapes project root")
    if not cwd.is_dir():
        raise ValueError("cwd is not a directory")
    return cwd


def execute_approved_shell(project: str | Path, rel: str, command: str) -> dict:
    command = (command or "").strip()
    if not command:
        return {"ok": False, "error": "command required", "exit_code": None, "output": ""}
    try:
        cwd = _safe_project_cwd(project, rel)
        proc = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SHELL_TIMEOUT,
            shell=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"command timed out after {SHELL_TIMEOUT}s",
            "exit_code": None,
            "output": "",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "exit_code": None, "output": ""}

    output_parts = []
    if proc.stdout:
        output_parts.append(proc.stdout.rstrip())
    if proc.stderr:
        output_parts.append("[stderr]\n" + proc.stderr.rstrip())
    output = "\n\n".join(output_parts) or "(no output)"
    output, truncated = clip_middle(output, SHELL_OUTPUT_LIMIT)
    return {
        "ok": True,
        "error": None,
        "exit_code": proc.returncode,
        "output": output,
        "truncated": truncated,
    }


@dataclass(frozen=True)
class RunSnapshot:
    run_id: str
    session_id: str
    project: str | None
    task: str
    provider_id: str
    status: str = "queued"


RUN_EVENT_TYPES = {
    "task_start",
    "turn",
    "tool",
    "info",
    "reply",
    "review",
    "shell_request",
    "shell_result",
    "teach_request",
    "task_done",
    "status",
}


class CodeyHTTPServer(ThreadingHTTPServer):
    """Keep routine browser disconnects out of the local server log."""

    def handle_error(self, request, client_address) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class State:
    def __init__(self, state_home: str | Path | None = None) -> None:
        self.state_home = Path(state_home) if state_home else None
        self.lock = threading.Lock()
        self.subscribers: list[queue.Queue[dict]] = []
        self.busy = False
        self.project: str | None = None
        self.task: str | None = None
        self.provider_id = DEFAULT_PROVIDER_ID
        self.status: str = "idle"
        self.last_summary: str | None = None
        self.last_stop_reason: str | None = None
        self.last_provider_failure: ProviderFailure | None = None
        self.stop_flag = threading.Event()
        self.pending_shell: dict[str, dict] = {}
        self.pending_teach: dict[str, dict] = {}
        self.change_trackers: dict[str, ChangeTracker] = {}
        self.conversations: dict[str, ConversationContext] = {}
        self.conversation_tokens: dict[str, object] = {}
        self.conversation_store_lock = threading.Lock()
        self.ui_state_store_lock = threading.Lock()
        self.provider_sessions: dict[str, str] = {}
        self.active_run: RunSnapshot | None = None
        self.last_terminal_event: dict | None = None
        self.last_shell_result: dict | None = None
        self.project_facts = (
            ProjectFactsStore(state_home) if state_home else ProjectFactsStore()
        )
        self.work_checkpoints = (
            WorkCheckpointStore(state_home) if state_home else WorkCheckpointStore()
        )
        self.provider_supervisor = (
            ProviderSupervisor(state_home) if state_home else ProviderSupervisor()
        )
        repair_runner = (
            self._run_self_repair_job
            if self.state_home is not None and self.state_home == DEFAULT_STATE_HOME
            else None
        )
        self.self_repair = (
            SelfRepairSupervisor(state_home, runner=repair_runner)
            if state_home
            else SelfRepairSupervisor(None)
        )
        self._self_repair_running = False
        self.conversation_store = (
            ConversationStore(state_home) if state_home else ConversationStore()
        )
        self.snapshot_store = (
            SnapshotStore(state_home) if state_home else SnapshotStore()
        )
        self.ui_state_store = (
            UiStateStore(state_home) if state_home else UiStateStore()
        )

    def load_ui_state(self) -> dict:
        with self.ui_state_store_lock:
            return self.ui_state_store.load()

    def save_ui_state(self, state: object) -> None:
        with self.ui_state_store_lock:
            self.ui_state_store.save(state)

    def visible_session_excerpt(self, session_id: str, current_request: str = "") -> str:
        with self.ui_state_store_lock:
            return self.ui_state_store.visible_session_excerpt(
                session_id,
                current_request=current_request,
            )

    def _save_conversation(
        self,
        session_id: str,
        token: object,
        context: ConversationContext,
    ) -> None:
        with self.conversation_store_lock:
            if self.conversation_tokens.get(session_id) is not token:
                return
            try:
                self.conversation_store.save(session_id, context)
            except (OSError, ValueError):
                pass

    def change_tracker_for(
        self,
        project: str | Path,
        *,
        persistent: bool,
    ) -> ChangeTracker:
        key = str(Path(project).expanduser().resolve())
        with self.lock:
            tracker = self.change_trackers.get(key)
            current_persistent = tracker is not None and tracker.store is not None
            if tracker is None or current_persistent != persistent:
                if not persistent:
                    if tracker is not None:
                        tracker.disable_persistence()
                    self.snapshot_store.delete(key)
                tracker = ChangeTracker(
                    key,
                    self.snapshot_store if persistent else None,
                )
                self.change_trackers[key] = tracker
            return tracker

    def reserve_run(
        self,
        *,
        session_id: str,
        project: str | None,
        task: str,
        provider_id: str,
    ) -> RunSnapshot | None:
        """Atomically reserve the single browser task slot."""
        with self.lock:
            if self.active_run is not None or self.busy:
                return None
            self.stop_flag.clear()
            run = RunSnapshot(
                run_id="run_" + uuid.uuid4().hex,
                session_id=session_id,
                project=project,
                task=task,
                provider_id=provider_id,
            )
            self.active_run = run
            self.busy = True
            self.project = project
            self.task = task
            self.provider_id = provider_id
            self.status = run.status
            return run

    def start_run(self, run_id: str) -> bool:
        with self.lock:
            if self.active_run is None or self.active_run.run_id != run_id:
                return False
            self.active_run = replace(self.active_run, status="running")
            self.status = "running"
            return True

    def set_run_status(self, status: str) -> None:
        with self.lock:
            if self.active_run is not None:
                self.active_run = replace(self.active_run, status=status)
            self.status = status

    def switch_run_provider(self, run_id: str, provider_id: str) -> bool:
        with self.lock:
            if self.active_run is None or self.active_run.run_id != run_id:
                return False
            self.active_run = replace(self.active_run, provider_id=provider_id)
            return True

    def release_run(self, run_id: str) -> None:
        with self.lock:
            if self.active_run is None or self.active_run.run_id != run_id:
                return
            self.active_run = None
            self.busy = False
            self.status = "idle"

    def finish_run(self, run_id: str, event: dict) -> bool:
        payload = dict(event)
        payload["run_id"] = run_id
        with self.lock:
            run = self.active_run
            if run is None or run.run_id != run_id:
                return False
            payload.setdefault("session_id", run.session_id)
            self.active_run = None
            self.busy = False
            self.last_terminal_event = payload
            self.last_summary = str(payload.get("summary") or "")
            self.last_stop_reason = str(payload.get("stop_reason") or "done")
            self.status = "error" if self.last_stop_reason == "error" else "done"
        self.emit(payload)
        return True

    def record_shell_result(self, event: dict) -> None:
        payload = dict(event)
        with self.lock:
            self.last_shell_result = payload
        self.emit(payload)

    def run_state_payload(self) -> dict:
        with self.lock:
            active = self.active_run
            terminal = dict(self.last_terminal_event) if self.last_terminal_event else None
            shell_result = dict(self.last_shell_result) if self.last_shell_result else None
            pending = self._pending_ui_event_locked(active)
            source = active
            run_id = source.run_id if source else str((terminal or {}).get("run_id") or "")
            session_id = source.session_id if source else str((terminal or {}).get("session_id") or "")
            return {
                "run_id": run_id,
                "session_id": session_id,
                "busy": active is not None,
                "run_status": active.status if active else self.status,
                "project": active.project if active else self.project,
                "task": active.task if active else self.task,
                "provider": active.provider_id if active else self.provider_id,
                "summary": self.last_summary,
                "stop_reason": self.last_stop_reason,
                "provider_failure": (
                    self.last_provider_failure.to_dict()
                    if self.last_provider_failure
                    else None
                ),
                "pending_event": pending,
                "last_terminal_event": terminal,
                "last_shell_result": shell_result,
            }

    def _pending_ui_event_locked(self, active: RunSnapshot | None) -> dict | None:
        candidates = [
            pending.get("ui_event")
            for pending in [
                *reversed(tuple(self.pending_teach.values())),
                *reversed(tuple(self.pending_shell.values())),
            ]
            if isinstance(pending.get("ui_event"), dict)
        ]
        if active is not None:
            for event in candidates:
                if event.get("run_id") == active.run_id:
                    return dict(event)
            return None
        return dict(candidates[0]) if candidates else None

    def emit(self, event: dict) -> None:
        with self.lock:
            payload = dict(event)
            active = self.active_run
            if (
                active is not None
                and payload.get("type") in RUN_EVENT_TYPES
                and not payload.get("run_id")
                and payload.get("session_id") in (None, active.session_id)
            ):
                payload["run_id"] = active.run_id
                payload.setdefault("session_id", active.session_id)
            for sub in list(self.subscribers):
                try:
                    sub.put_nowait(payload)
                except Exception:
                    pass

    def subscribe(self) -> queue.Queue[dict]:
        q: queue.Queue[dict] = queue.Queue(maxsize=1000)
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[dict]) -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def get_provider(self, provider_id: str = DEFAULT_PROVIDER_ID):
        self.set_run_status("connecting")
        self.emit({"type": "status", "status": "connecting"})
        provider = connect_provider(provider_id)
        self.set_run_status("running")
        self.emit({
            "type": "providers",
            "providers": provider_status_update(provider_id, True),
        })
        return provider

    def kick_self_repair(self) -> bool:
        """Run at most one queued adapter repair while the main task slot is idle."""
        supervisor = getattr(self, "self_repair", None)
        if supervisor is None or not supervisor.pending():
            return False
        has_due_work = getattr(supervisor, "has_due_work", None)
        if callable(has_due_work) and not has_due_work():
            return False
        with self.lock:
            if self.busy or self._self_repair_running:
                return False
            self._self_repair_running = True

        def _worker() -> None:
            try:
                supervisor.run_pending_once()
            finally:
                with self.lock:
                    self._self_repair_running = False

        threading.Thread(target=_worker, name="codey-self-repair", daemon=True).start()
        return True

    def _run_self_repair_job(self, job: SelfRepairJob) -> AdapterRepairResult:
        if self.state_home is None:
            return AdapterRepairResult(False, job.provider_id, error="self-repair state is unavailable")
        return run_self_repair_worker(
            job,
            helper_ids=self._self_repair_model_candidates(job.provider_id),
            state_home=self.state_home,
            source_root=Path(__file__).resolve().parents[1],
        )

    def _self_repair_model_candidates(self, broken_provider_id: str) -> tuple[str, ...]:
        broken = str(broken_provider_id or "").strip().lower()
        ordered = self.provider_failover_order()
        return tuple(
            provider_id
            for provider_id in ordered
            if provider_id != broken and self.provider_supervisor.is_available(provider_id)
        )

    def provider_failover_order(self) -> tuple[str, ...]:
        """Prefer already-open sibling tabs, then keep the registry order stable."""
        try:
            statuses = provider_tab_availability()
        except Exception:
            statuses = {}
        opened = tuple(
            provider_id
            for provider_id in PROVIDER_LABELS
            if statuses.get(provider_id)
        )
        return opened + tuple(
            provider_id
            for provider_id in PROVIDER_LABELS
            if provider_id not in opened
        )

    def conversation_for(self, session_id: str) -> ConversationContext:
        with self.lock:
            context = self.conversations.pop(session_id, None)
            if context is None:
                if len(self.conversations) >= MAX_CONVERSATION_STATES:
                    oldest = next(iter(self.conversations))
                    evicted = self.conversations.pop(oldest)
                    evicted.on_change = None
                    with self.conversation_store_lock:
                        self.conversation_tokens.pop(oldest, None)
                with self.conversation_store_lock:
                    token = object()
                    self.conversation_tokens[session_id] = token
                    context = self.conversation_store.load(session_id)
                context.on_change = lambda value, owner=session_id, owner_token=token: (
                    self._save_conversation(owner, owner_token, value)
                )
            self.conversations[session_id] = context
            return context

    def forget_conversation(self, session_id: str) -> None:
        with self.lock:
            context = self.conversations.pop(session_id, None)
            if context is not None:
                context.on_change = None
            if (
                self.last_terminal_event is not None
                and self.last_terminal_event.get("session_id") == session_id
            ):
                self.last_terminal_event = None
                self.last_summary = ""
                self.last_stop_reason = ""
            if (
                self.last_shell_result is not None
                and self.last_shell_result.get("session_id") == session_id
            ):
                self.last_shell_result = None
            for provider_id, owner in list(self.provider_sessions.items()):
                if owner == session_id:
                    self.provider_sessions.pop(provider_id)
            with self.conversation_store_lock:
                self.conversation_tokens.pop(session_id, None)
                self.conversation_store.delete(session_id)

    def provider_session_changed(self, provider_id: str, session_id: str) -> bool:
        with self.lock:
            return self.provider_sessions.get(provider_id) != session_id

    def set_provider_session(self, provider_id: str, session_id: str | None) -> None:
        with self.lock:
            if session_id:
                self.provider_sessions[provider_id] = session_id
            else:
                self.provider_sessions.pop(provider_id, None)

    def handle_control_teach(self, request: provider_controls.ControlTeachRequest):
        while True:
            teach_id = "teach_" + uuid.uuid4().hex[:12]
            token = provider_controls.start_click_capture(request.page)
            pending = {
                "id": teach_id,
                "request": request,
                "token": token,
                "event": threading.Event(),
                "cancelled": False,
            }
            with self.lock:
                run_id = (
                    self.active_run.run_id
                    if self.active_run is not None
                    and self.active_run.session_id == request.session_id
                    else ""
                )
                pending["ui_event"] = {
                    "type": "teach_request",
                    "run_id": run_id,
                    "session_id": request.session_id,
                    "id": teach_id,
                    "text": request.message,
                }
                self.pending_teach[teach_id] = pending
            self.emit(pending["ui_event"])
            if not pending["event"].wait(CONTROL_TEACH_TIMEOUT):
                with self.lock:
                    self.pending_teach.pop(teach_id, None)
                provider_controls.cancel_click_capture(request.page)
                raise TimeoutError("Timed out waiting for Resume")
            if pending.get("cancelled"):
                provider_controls.cancel_click_capture(request.page)
                raise provider_controls.ControlTeachCancelled("control teaching was cancelled")
            try:
                captured = provider_controls.finish_click_capture(
                    request.page,
                    token,
                    request.action,
                    timeout=1.0,
                )
                return provider_controls.resolve_captured_control(request, captured)
            except ValueError:
                continue
            finally:
                with self.lock:
                    self.pending_teach.pop(teach_id, None)

    def handle_profile_doctor(
        self,
        request: profile_doctor.ProfileDoctorRequest,
    ) -> str | None:
        """Try healthy sibling tabs within one bounded recovery deadline."""
        cancellation.check()
        deadline = time.monotonic() + PROFILE_DOCTOR_TIMEOUT
        for provider_id in reviewer_candidates(request.provider_id)[:3]:
            cancellation.check()
            if not self.provider_supervisor.is_available(provider_id):
                continue
            if time.monotonic() >= deadline:
                return None
            helper = borrow_open_provider(provider_id, request.page)
            if helper is None:
                continue
            try:
                helper.new_chat(timeout=max(0.1, deadline - time.monotonic()))
            except cancellation.TaskCancelled:
                helper.close()
                raise
            except Exception:
                helper.close()
                continue
            self.set_provider_session(provider_id, None)
            try:
                selected = profile_doctor.choose_candidate(
                    request,
                    lambda prompt: helper.send(
                        prompt,
                        timeout=max(0.1, deadline - time.monotonic()),
                    ),
                )
            except cancellation.TaskCancelled:
                raise
            except Exception:
                continue
            finally:
                helper.close()
            if selected:
                return selected
        return None

    def handle_flow_recovery(
        self,
        request: provider_flow.FlowRecoveryRequest,
    ) -> str | None:
        """Ask healthy siblings to choose only among fixed flow predicates."""
        cancellation.check()
        deadline = time.monotonic() + PROFILE_DOCTOR_TIMEOUT
        for provider_id in reviewer_candidates(request.provider_id)[:3]:
            cancellation.check()
            if not self.provider_supervisor.is_available(provider_id):
                continue
            if time.monotonic() >= deadline:
                return None
            helper = borrow_open_provider(provider_id, request.page)
            if helper is None:
                continue
            with provider_controls.suppress_assistance():
                try:
                    helper.new_chat(timeout=max(0.1, deadline - time.monotonic()))
                except cancellation.TaskCancelled:
                    helper.close()
                    raise
                except Exception:
                    helper.close()
                    continue
                self.set_provider_session(provider_id, None)
                try:
                    selected = provider_flow.choose_candidate(
                        request,
                        lambda prompt: helper.send(
                            prompt,
                            timeout=max(0.1, deadline - time.monotonic()),
                        ),
                    )
                except cancellation.TaskCancelled:
                    raise
                except Exception:
                    continue
                finally:
                    helper.close()
            if selected:
                return selected
        return None


STATE = State(DEFAULT_STATE_HOME)
provider_controls.set_teach_handler(STATE.handle_control_teach)
provider_controls.set_doctor_handler(STATE.handle_profile_doctor)
provider_flow.set_recovery_handler(STATE.handle_flow_recovery)


def pick_folder(mode: str = "open", initial: str | None = None) -> str | None:
    """Open a native folder picker and return the selected absolute path.

    Browsers cannot expose an arbitrary local folder path to JavaScript, so the
    local server owns this action.  Tkinter ships with Python and gives us the
    standard Windows folder dialog without adding dependencies.
    """
    import tkinter as tk
    from tkinter import filedialog

    title = "Select Existing Project Folder"
    mustexist = True
    if mode == "new":
        title = "Create or Select Project Folder"
        mustexist = False

    initial_path = Path(initial).expanduser() if initial else Path.home()
    if not initial_path.exists():
        initial_path = Path.home()
    initialdir = str(initial_path)
    with FOLDER_DIALOG_LOCK:
        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
            selected = filedialog.askdirectory(
                parent=root,
                title=title,
                initialdir=initialdir,
                mustexist=mustexist,
            )
        finally:
            root.destroy()

    if not selected:
        return None
    path = Path(selected).resolve()
    if mode == "new":
        path.mkdir(parents=True, exist_ok=True)
    return str(path)


# ----------------------------------------------------------- task runner ---

def _run_task(
    session_id: str,
    project: str | None,
    task: str,
    max_turns: int,
    continue_task: bool,
    provider_id: str,
    run_id: str = "",
) -> None:
    if not run_id:
        reserved = STATE.reserve_run(
            session_id=session_id,
            project=project,
            task=task,
            provider_id=provider_id,
        )
        if reserved is None:
            return
        run_id = reserved.run_id
    runner = TaskRunner(
        STATE,
        agent_run=agent_run,
        collect_changes=collect_changes,
        run_review=_run_review,
        capture_provider_failure=capture_provider_failure,
        run_consensus=_run_consensus,
        run_project_audit=_run_project_audit,
        project_facts=STATE.project_facts,
        work_checkpoints=STATE.work_checkpoints,
        is_git_repository=is_git_repository,
        review_fix_turns=REVIEW_FIX_TURNS,
        review_log_lines=REVIEW_LOG_LINES,
    )
    try:
        runner.run(TaskRequest(
            session_id=session_id,
            project=project,
            task=task,
            max_turns=max_turns,
            continue_task=continue_task,
            provider_id=provider_id,
            run_id=run_id,
        ))
    finally:
        STATE.kick_self_repair()


def _submit_task(
    session_id: str,
    project: str | None,
    task: str,
    max_turns: int,
    continue_task: bool,
    provider_id: str,
) -> str | None:
    reserved = STATE.reserve_run(
        session_id=session_id,
        project=project,
        task=task,
        provider_id=provider_id,
    )
    if reserved is None:
        return None
    try:
        submit_browser_task(
            _run_task,
            session_id,
            project,
            task,
            max_turns,
            continue_task,
            provider_id,
            reserved.run_id,
        )
    except Exception:
        STATE.release_run(reserved.run_id)
        raise
    return reserved.run_id

# ------------------------------------------------------------ http layer ---

class Handler(BaseHTTPRequestHandler):
    server_version = f"Codey/{__version__}"

    def log_message(self, fmt, *args):
        # Quiet the default access log.
        pass

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, ctype: str) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            return
        if url.path == "/icon.ico":
            icon = WEB_DIR / "icon.ico"
            if icon.is_file():
                self._send_file(icon, "image/x-icon")
            else:
                self.send_response(404)
                self.end_headers()
            return
        if url.path == "/api/state":
            self._send_json(200, STATE.run_state_payload())
            return
        if url.path == "/api/ui_state":
            self._send_json(200, {"ok": True, "state": STATE.load_ui_state()})
            return
        if url.path == "/api/providers":
            try:
                statuses = provider_availability()
            except Exception:
                statuses = {}
            self._send_json(200, {
                "default": DEFAULT_PROVIDER_ID,
                "providers": provider_payload(statuses),
            })
            return
        if url.path == "/api/changes":
            query = parse_qs(url.query)
            project = (query.get("project") or [""])[0].strip()
            key = str(Path(project).expanduser().resolve()) if project else ""
            tracker = (
                STATE.change_tracker_for(key, persistent=not is_git_repository(key))
                if key
                else None
            )
            payload = collect_changes(project, tracker)
            self._send_json(200 if payload.get("ok") else 400, payload)
            return
        if url.path == "/api/events":
            self._sse()
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            self._send_json(400, {"error": "invalid json"})
            return

        if url.path == "/api/ui_state":
            state = body.get("state") if isinstance(body, dict) else None
            try:
                STATE.save_ui_state(state)
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            except OSError as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
                return
            self._send_json(200, {"ok": True})
            return
        if url.path == "/api/run":
            session_id = str(body.get("session_id") or "").strip() or "default"
            project = (body.get("project") or "").strip() or None
            task = (body.get("task") or "").strip()
            continue_task = bool(body.get("continue_task"))
            provider_id = str(body.get("provider") or DEFAULT_PROVIDER_ID).strip().lower()
            try:
                max_turns = int(body.get("max_turns") or DEFAULT_MAX_TURNS)
            except (TypeError, ValueError):
                self._send_json(400, {"error": "invalid max_turns"})
                return
            max_turns = max(1, min(max_turns, 500))
            if not task:
                self._send_json(400, {"error": "task required"})
                return
            if provider_id not in PROVIDER_LABELS:
                self._send_json(400, {"error": f"unsupported provider: {provider_id}"})
                return
            if project:
                Path(project).mkdir(parents=True, exist_ok=True)
            try:
                run_id = _submit_task(
                    session_id,
                    project,
                    task,
                    max_turns,
                    continue_task,
                    provider_id,
                )
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
                return
            if run_id is None:
                self._send_json(409, {"error": "busy"})
                return
            self._send_json(200, {"ok": True, "run_id": run_id})
            return
        if url.path == "/api/pick_folder":
            mode = str(body.get("mode") or "open").strip().lower()
            if mode not in {"open", "new"}:
                self._send_json(400, {"error": "invalid mode"})
                return
            initial = str(body.get("initial") or "").strip() or None
            try:
                path = pick_folder(mode=mode, initial=initial)
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
                return
            if not path:
                self._send_json(200, {"ok": False, "cancelled": True})
                return
            self._send_json(200, {"ok": True, "path": path, "name": Path(path).name or path})
            return
        if url.path == "/api/changes":
            project = (body.get("project") or "").strip()
            key = str(Path(project).expanduser().resolve()) if project else ""
            tracker = (
                STATE.change_tracker_for(key, persistent=not is_git_repository(key))
                if key
                else None
            )
            payload = collect_changes(project, tracker)
            self._send_json(200 if payload.get("ok") else 400, payload)
            return
        if url.path == "/api/changes/restore":
            project = (body.get("project") or "").strip()
            if not project:
                self._send_json(400, {"ok": False, "error": "project required"})
                return
            paths = body.get("paths")
            if paths is not None and not isinstance(paths, list):
                self._send_json(400, {"ok": False, "error": "paths must be a list"})
                return
            clean_paths = [str(path) for path in paths] if paths is not None else None
            key = str(Path(project).expanduser().resolve())
            tracker = STATE.change_tracker_for(
                key,
                persistent=not is_git_repository(key),
            )
            if not tracker.has_snapshots:
                tracker = None
            status, payload = restore_snapshot_changes(project, tracker, clean_paths)
            self._send_json(status, payload)
            return
        if url.path == "/api/shell_approval":
            approval_id = str(body.get("id") or "").strip()
            approved = body.get("approved") is True
            with STATE.lock:
                pending = STATE.pending_shell.pop(approval_id, None)
            if not pending:
                self._send_json(404, {"error": "approval not found"})
                return
            session_id = pending["session_id"]
            command = pending["command"]
            if not approved:
                event = {
                    "type": "shell_result",
                    "run_id": pending.get("run_id") or "",
                    "session_id": session_id,
                    "id": approval_id,
                    "approved": False,
                    "command": command,
                    "cwd": pending["cwd"],
                    "output": "用户已拒绝执行该命令。",
                    "exit_code": None,
                }
                STATE.record_shell_result(event)
                self._send_json(200, {"ok": True, "approved": False, "event": event})
                return

            result = execute_approved_shell(pending["project"], pending["cwd"], command)
            event = {
                "type": "shell_result",
                "run_id": pending.get("run_id") or "",
                "session_id": session_id,
                "id": approval_id,
                "approved": True,
                "command": command,
                "cwd": pending["cwd"],
                "output": result.get("output") or result.get("error") or "",
                "exit_code": result.get("exit_code"),
                "ok": result.get("ok"),
                "truncated": bool(result.get("truncated")),
            }
            STATE.record_shell_result(event)
            continued = False
            if pending.get("continue_after"):
                truncation_note = (
                    "\nShell output was truncated. Do not assume omitted content "
                    "is clean; inspect narrower output if needed.\n"
                    if result.get("truncated")
                    else ""
                )
                continuation = (
                    "Continue the interrupted task in this same conversation.\n"
                    "The user approved and ran this shell command:\n"
                    f"{command}\n\n"
                    f"Exit code: {result.get('exit_code')}\n"
                    "Output:\n"
                    f"{result.get('output') or result.get('error') or '(no output)'}\n\n"
                    f"{truncation_note}"
                    "Use this result to continue the original task. If the task is complete,"
                    " reply with a JSON done tool call."
                )
                continuation_run = _submit_task(
                    session_id,
                    pending["project"],
                    continuation,
                    int(pending["max_turns"]),
                    True,
                    pending.get("provider") or DEFAULT_PROVIDER_ID,
                )
                continued = continuation_run is not None
            self._send_json(200, {
                "ok": True,
                "approved": True,
                "continued": continued,
                "result": result,
                "event": event,
            })
            return
        if url.path == "/api/teach/resume":
            teach_id = str(body.get("id") or "").strip()
            with STATE.lock:
                pending = STATE.pending_teach.get(teach_id)
            if not pending:
                self._send_json(404, {"error": "pause not found"})
                return
            pending["event"].set()
            self._send_json(200, {"ok": True})
            return
        if url.path == "/api/new_chat":
            session_id = str(body.get("session_id") or "").strip()
            if session_id:
                STATE.forget_conversation(session_id)
            self._send_json(200, {"ok": True})
            return
        if url.path == "/api/stop":
            STATE.stop_flag.set()
            with STATE.lock:
                pending_teach = list(STATE.pending_teach.values())
            for pending in pending_teach:
                pending["cancelled"] = True
                pending["event"].set()
            self._send_json(200, {"ok": True})
            return
        self.send_response(404)
        self.end_headers()

    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = STATE.subscribe()
        try:
            q.put_nowait({"type": "hello", "status": STATE.status})
            while True:
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    try:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                    except Exception:
                        break
                    continue
                data = json.dumps(ev, ensure_ascii=False)
                try:
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except Exception:
                    break
        finally:
            STATE.unsubscribe(q)


def _wait_for_manual_browser(url: str, exc: Exception) -> None:
    print(f"[codey] Could not open native window: {exc}")
    print(f"[codey] Open this URL in your browser instead: {url}")
    if sys.platform == "win32":
        print("[codey] If needed, install Microsoft Edge WebView2 Runtime.")
    print("[codey] Press Ctrl+C to stop.")
    while True:
        time.sleep(3600)


def serve(host: str = "127.0.0.1", port: int = 5173) -> None:
    httpd = CodeyHTTPServer((host, port), Handler)
    actual_port = httpd.server_address[1]
    url = f"http://{host}:{actual_port}/"
    print(f"[codey] UI ready: {url}")

    def _run_httpd() -> None:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass

    threading.Thread(target=_run_httpd, daemon=True).start()

    def _run_webview() -> None:
        import webview

        icon = WEB_DIR / "icon.ico"
        webview.create_window("Codey", url, width=1380, height=900)
        start_kwargs = {
            "private_mode": False,
            "storage_path": str(DEFAULT_STATE_HOME / "webview"),
        }
        if icon.is_file():
            start_kwargs["icon"] = str(icon)
        webview.start(**start_kwargs)

    try:
        _run_webview()
    except KeyboardInterrupt:
        print("\n[codey] shutting down")
    except Exception as exc:
        try:
            _wait_for_manual_browser(url, exc)
        except KeyboardInterrupt:
            print("\n[codey] shutting down")
    finally:
        httpd.shutdown()
