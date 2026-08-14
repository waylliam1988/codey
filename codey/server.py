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
    GET  /api/ghost/summary query {session_id, project} → returns bounded
                          local context summary
    POST /api/ghost/action body {action, ...} → reviews or deletes local
                          context state without provider/tool execution
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
from codey.ghost.affinity import GhostAffinityStore
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.continuity import GhostContinuityStore
from codey.ghost.hebbian import GhostHebbianStore
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.router import GhostRouteStore
from codey.ghost.sleep import GhostSleepStore
from codey.managed_outputs import ManagedOutputStore
from codey.ghost.store import GhostSignalStore
from codey.ghost.work_queue import GhostWorkQueueStore
from codey.knowledge.concepts import ConceptGraphBuilder
from codey.knowledge.store import KnowledgeStore
from codey.knowledge.unified_graph import UnifiedResearchGraphBuilder
from codey.research.advisors import EvidencePack, run_research_advisors
from codey.providers import (
    DEFAULT_PROVIDER_ID,
    PROVIDER_LABELS,
    borrow_open_provider,
    connect_existing_provider,
    connect_fresh_provider_tab,
    connect_provider,
    provider_tab_availability,
    warm_provider_tabs,
)
from codey.providers.local_openai import (
    load_local_config,
    local_config_payload,
    probe_local_endpoint,
    save_local_config,
)
from codey.provider_capabilities import rank_providers
from codey.provider_diagnostics import ProviderFailure, capture_provider_failure
from codey.provider_supervisor import ProviderSupervisor
from codey.adapter_repair import AdapterRepairResult
from codey.self_repair import SelfRepairJob, SelfRepairSupervisor
from codey.self_repair_worker import run_self_repair_worker
from codey.project_facts import ProjectFactsStore
from codey.project_task_context import safe_verification_candidates
from codey.run_ledger import RunLedgerStore
from codey.run_trace import RunTraceStore
from codey.shell_followup import ShellFollowupInput, render_shell_followup
from codey.setup_context import safe_setup_context
from codey.work_checkpoint import WorkCheckpointStore
from codey.review import (
    ReviewResult,
    parse_review_with_repair,
    render_review_prompt,
)
from codey.review_impact_map import safe_review_impact_map
from codey.task_runner import TaskRequest, TaskRunner
from codey.text_budget import clip_middle
from codey.ui_state_store import UiStateStore

WEB_DIR = Path(__file__).parent / "web"
WEB_ASSET_DIR = WEB_DIR / "assets"
WEB_ASSET_TYPES = {
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


def resolve_web_asset(url_path: str) -> tuple[Path, str] | None:
    """Resolve /assets/* to a real file inside codey/web/assets, or None.

    Only .js/.css files are served. Anything that escapes the assets
    directory (e.g. ../) or is not a regular file resolves to None.
    """
    prefix = "/assets/"
    if not url_path.startswith(prefix):
        return None
    name = url_path[len(prefix):]
    ctype = WEB_ASSET_TYPES.get(Path(name).suffix.lower())
    if not ctype:
        return None
    path = (WEB_ASSET_DIR / name).resolve()
    try:
        path.relative_to(WEB_ASSET_DIR.resolve())
    except ValueError:
        return None
    if not path.is_file():
        return None
    return path, ctype


FOLDER_DIALOG_LOCK = threading.Lock()
SHELL_TIMEOUT = 120
SHELL_OUTPUT_LIMIT = 24_000
REVIEW_TIMEOUT = 300.0
REVIEW_FIX_TURNS = 12
REVIEW_LOG_LINES = 80
CONTROL_TEACH_TIMEOUT = 300.0
PROFILE_DOCTOR_TIMEOUT = 90.0


def _profile_doctor_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    return max(0.1, min(PROFILE_DOCTOR_TIMEOUT, remaining))
MAX_CONVERSATION_STATES = 32


def reviewer_candidates(writer_id: str) -> tuple[str, ...]:
    writer = (writer_id or DEFAULT_PROVIDER_ID).strip().lower()
    supervisor = getattr(globals().get("STATE"), "provider_supervisor", None)
    candidates = tuple(
        provider_id
        for provider_id in PROVIDER_LABELS
        if provider_id != writer
        and provider_id != "local"
        and (supervisor is None or supervisor.is_available(provider_id))
    )
    return rank_providers(candidates, mode="review")


def review_label(provider_id: str) -> str:
    return PROVIDER_LABELS.get(provider_id, provider_id)


def provider_availability() -> dict[str, bool]:
    return provider_availability_from_statuses(provider_tab_availability())


def provider_availability_from_statuses(statuses: dict[str, bool]) -> dict[str, bool]:
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


def _run_provider_warmup(runner=warm_provider_tabs) -> None:
    try:
        raw_statuses = runner()
        statuses = provider_availability_from_statuses(raw_statuses)
        STATE.emit({"type": "providers", "providers": provider_payload(statuses)})
    except Exception:
        pass


def _start_provider_warmup(runner=warm_provider_tabs) -> None:
    submit_browser_task(_run_provider_warmup, runner)


def _emit_review(session_id: str, text: str) -> None:
    STATE.emit({"type": "review", "session_id": session_id, "text": text})


def _run_review_attempt(
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
        with provider_controls.suppress_assistance():
            reply = reviewer.send(prompt, timeout=REVIEW_TIMEOUT)
            review = parse_review_with_repair(
                reply,
                lambda repair: reviewer.send(repair, timeout=REVIEW_TIMEOUT),
                changes=changes,
            )
        label = review_label(reviewer_id)
        prefix = f"{label} self-review" if self_review else label
        if review.approved:
            _emit_review(session_id, f"{prefix} approved")
        else:
            _emit_review(session_id, f"{prefix} suggested changes")
        return reviewer_id, review
    finally:
        try:
            reviewer.close()
        except Exception:
            pass


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
    review_impact_map: str | None = None,
    execution_evidence: str = "",
) -> tuple[str, ReviewResult] | None:
    cancellation.check()
    last_error: Exception | None = None
    if review_impact_map is None:
        review_impact_map = safe_review_impact_map(project, changes)
    for reviewer_id in reviewer_candidates(writer_id):
        cancellation.check()
        try:
            reviewer = connect_existing_provider(reviewer_id)
            STATE.set_provider_session(reviewer_id, None)
            return _run_review_attempt(
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
            )
        except cancellation.TaskCancelled:
            raise
        except Exception as exc:
            last_error = exc
    cancellation.check()
    try:
        reviewer_id = (writer_id or DEFAULT_PROVIDER_ID).strip().lower()
        reviewer = connect_fresh_provider_tab(reviewer_id)
        return _run_review_attempt(
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
        )
    except cancellation.TaskCancelled:
        raise
    except Exception as exc:
        last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("no review model available")


def _connect_consensus_provider(selected_provider, provider_id: str):
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


def _run_research_advisors(
    *,
    selected_provider,
    selected_provider_id: str,
    pack: EvidencePack,
) -> tuple[ConsensusAdvice, ...]:
    return run_research_advisors(
        selected_provider_id=selected_provider_id,
        provider_ids=tuple(PROVIDER_LABELS),
        provider_labels=PROVIDER_LABELS,
        availability=provider_availability,
        connect_existing=lambda provider_id: _connect_consensus_provider(
            selected_provider,
            provider_id,
        ),
        clear_provider_session=lambda provider_id: STATE.set_provider_session(provider_id, None),
        pack=pack,
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


def build_shell_approval_continuation(
    *,
    command: str,
    result: dict,
    post_approval_instructions: str = "",
    setup_context: str = "",
    followup_hints: str = "",
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
        "Use this result to continue the original task. If the task is complete,"
        " reply with a JSON done tool call."
    )


def _shell_continuation_setup_context(pending: dict) -> str:
    if pending.get("risk_label") not in {
        "dependency_install",
        "system_install",
        "external_source",
        "dev_server",
    }:
        return ""
    return safe_setup_context(pending["project"])


def _shell_followup_verification_candidates(project: str | Path, risk_label: object):
    if risk_label not in {"dependency_install", "dev_server", "publish"}:
        return ()
    return safe_verification_candidates(project)


def _shell_followup_hints(
    *,
    pending: dict,
    result: dict,
) -> str:
    return render_shell_followup(ShellFollowupInput(
        risk_label=str(pending.get("risk_label") or "generic"),
        exit_code=result.get("exit_code"),
        output=str(result.get("output") or result.get("error") or ""),
        truncated=bool(result.get("truncated")),
        verification_candidates=_shell_followup_verification_candidates(
            pending["project"],
            pending.get("risk_label"),
        ),
    ))


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
        self.research_changes: dict[str, object] = {}
        self._knowledge_rebuild_running = False
        self._knowledge_rebuild_pending = False
        self._ghost_sleep_running = False
        self._ghost_sleep_pending = False
        self._ghost_sleep_pending_payload: dict[str, object] | None = None
        self._ghost_sleep_thread: threading.Thread | None = None
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
        resolved_state_home = Path(state_home).expanduser().resolve() if state_home else None
        self.knowledge_store = (
            KnowledgeStore(Path(state_home) / "vault")
            if resolved_state_home == DEFAULT_STATE_HOME.expanduser().resolve()
            else None
        )
        self.work_checkpoints = (
            WorkCheckpointStore(state_home) if state_home else WorkCheckpointStore()
        )
        self.run_ledgers = RunLedgerStore(state_home) if state_home else None
        self.run_traces = RunTraceStore(state_home) if state_home else None
        self.managed_outputs = ManagedOutputStore(state_home) if state_home else None
        self.ghost_inbox = GhostInboxStore(state_home) if state_home else None
        self.ghost_hebbian = GhostHebbianStore(state_home) if state_home else None
        self.ghost_continuity = GhostContinuityStore(state_home) if state_home else None
        self.ghost_router = GhostRouteStore(state_home) if state_home else None
        self.ghost_sleep = GhostSleepStore(state_home) if state_home else None
        self.ghost_work_queue = GhostWorkQueueStore(state_home) if state_home else None
        self.ghost_affinity = GhostAffinityStore(state_home) if state_home else None
        self.ghost_signals = GhostSignalStore(state_home) if state_home else None
        self.ghost_learning_provider_factory = None
        self.ghost_router_provider_factory = None
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

    def record_research_changes(self, run_id: str, changes: object) -> None:
        with self.lock:
            self.research_changes[run_id] = changes
            if len(self.research_changes) > 32:
                for key in list(self.research_changes)[:-32]:
                    self.research_changes.pop(key, None)

    def restore_research_changes(self, run_id: str) -> dict:
        with self.lock:
            changes = self.research_changes.get(run_id)
        if changes is None:
            return {"ok": False, "error": "research changes not found"}
        result = changes.restore_result()
        if self.knowledge_store is not None:
            self._schedule_knowledge_rebuild()
        if result.ok:
            with self.lock:
                self.research_changes.pop(run_id, None)
        return {
            "ok": result.ok,
            "restored": result.restored,
            "conflicts": result.conflicts,
            "error": result.error,
        }

    def _schedule_knowledge_rebuild(self) -> None:
        if self.knowledge_store is None:
            return
        with self.lock:
            if self._knowledge_rebuild_running:
                self._knowledge_rebuild_pending = True
                return
            self._knowledge_rebuild_running = True
            self._knowledge_rebuild_pending = False
        threading.Thread(
            target=self._run_knowledge_rebuild,
            name="codey-knowledge-rebuild",
            daemon=True,
        ).start()

    def _run_knowledge_rebuild(self) -> None:
        while True:
            store = self.knowledge_store
            if store is not None:
                try:
                    store.rebuild()
                except Exception:
                    pass
            with self.lock:
                if self._knowledge_rebuild_pending:
                    self._knowledge_rebuild_pending = False
                    continue
                self._knowledge_rebuild_running = False
                return

    def kick_ghost_sleep(
        self,
        *,
        trigger: str = "post_turn",
        run_id: str = "",
        session_id: str = "",
        project: str = "",
        run_projection: object = None,
    ) -> bool:
        sleep = getattr(self, "ghost_sleep", None)
        if sleep is None:
            return False
        inbox = getattr(self, "ghost_inbox", None)
        if inbox is not None:
            try:
                if not inbox.learning_enabled():
                    return False
            except Exception:
                return False
        payload = {
            "trigger": trigger,
            "run_id": run_id,
            "session_id": session_id,
            "project": project,
            "run_projection": run_projection,
        }
        with self.lock:
            if self.busy:
                self._ghost_sleep_pending = True
                self._ghost_sleep_pending_payload = payload
                return False
            if self._ghost_sleep_running:
                self._ghost_sleep_pending = True
                self._ghost_sleep_pending_payload = payload
                return False
            self._ghost_sleep_running = True
            self._ghost_sleep_pending = False
            self._ghost_sleep_pending_payload = None

        def _worker() -> None:
            current_payload = payload
            while True:
                try:
                    sleep.run_once(
                        inbox_store=getattr(self, "ghost_inbox", None),
                        hebbian_store=getattr(self, "ghost_hebbian", None),
                        continuity_store=getattr(self, "ghost_continuity", None),
                        work_queue_store=getattr(self, "ghost_work_queue", None),
                        affinity_store=getattr(self, "ghost_affinity", None),
                        router_store=getattr(self, "ghost_router", None),
                        knowledge_store=getattr(self, "knowledge_store", None),
                        run_projection=current_payload.get("run_projection"),
                        trigger=str(current_payload.get("trigger") or "post_turn"),
                        run_id=str(current_payload.get("run_id") or ""),
                        session_id=str(current_payload.get("session_id") or ""),
                        project=str(current_payload.get("project") or ""),
                        should_cancel=lambda: self.busy or self.stop_flag.is_set(),
                    )
                except Exception:
                    pass
                with self.lock:
                    if self._ghost_sleep_pending and not self.busy:
                        current_payload = self._ghost_sleep_pending_payload or current_payload
                        self._ghost_sleep_pending = False
                        self._ghost_sleep_pending_payload = None
                        continue
                    self._ghost_sleep_running = False
                    self._ghost_sleep_pending_payload = None
                    if self._ghost_sleep_thread is threading.current_thread():
                        self._ghost_sleep_thread = None
                    return

        thread = threading.Thread(target=_worker, name="codey-ghost-sleep", daemon=True)
        with self.lock:
            self._ghost_sleep_thread = thread
        thread.start()
        return True

    def wait_for_ghost_sleep(self, timeout: float | None = None) -> bool:
        with self.lock:
            thread = self._ghost_sleep_thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def run_state_payload(self) -> dict:
        with self.lock:
            active = self.active_run
            terminal = dict(self.last_terminal_event) if self.last_terminal_event else None
            shell_result = dict(self.last_shell_result) if self.last_shell_result else None
            pending = self._pending_ui_event_locked(active)
            source = active
            run_id = source.run_id if source else str((terminal or {}).get("run_id") or "")
            session_id = source.session_id if source else str((terminal or {}).get("session_id") or "")
            research_restore_runs = sorted(self.research_changes)
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
                "research_restore_runs": research_restore_runs,
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
                except queue.Full:
                    try:
                        sub.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        sub.put_nowait(payload)
                    except Exception:
                        pass
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
            if provider_id != "local" and statuses.get(provider_id)
        )
        return opened + tuple(
            provider_id
            for provider_id in PROVIDER_LABELS
            if provider_id != "local" and provider_id not in opened
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
        continuity = getattr(self, "ghost_continuity", None)
        if continuity is not None:
            try:
                continuity.delete_scope("session", session_id=session_id)
            except Exception:
                pass
        router = getattr(self, "ghost_router", None)
        if router is not None:
            try:
                router.delete_scope("session", session_id=session_id)
            except Exception:
                pass
        sleep = getattr(self, "ghost_sleep", None)
        if sleep is not None:
            try:
                sleep.delete_scope("session", session_id=session_id)
            except Exception:
                pass
        work_queue = getattr(self, "ghost_work_queue", None)
        if work_queue is not None:
            try:
                work_queue.delete_scope("session", session_id=session_id)
            except Exception:
                pass
        affinity = getattr(self, "ghost_affinity", None)
        if affinity is not None:
            try:
                affinity.delete_scope("session", session_id=session_id)
            except Exception:
                pass
        traces = getattr(self, "run_traces", None)
        if traces is not None:
            try:
                traces.delete_session(session_id)
            except Exception:
                pass

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
                helper.new_chat(timeout=_profile_doctor_timeout(deadline))
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
                        timeout=_profile_doctor_timeout(deadline),
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
                    helper.new_chat(timeout=_profile_doctor_timeout(deadline))
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
                            timeout=_profile_doctor_timeout(deadline),
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
STATE.ghost_learning_provider_factory = connect_fresh_provider_tab
STATE.ghost_router_provider_factory = connect_fresh_provider_tab
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


def _should_wait_for_local_ghost_sleep(state_home: Path | None) -> bool:
    if state_home is None:
        return False
    try:
        return state_home.expanduser().resolve() != DEFAULT_STATE_HOME.expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return True


# ----------------------------------------------------------- task runner ---

def _run_task(
    session_id: str,
    project: str | None,
    task: str,
    max_turns: int,
    continue_task: bool,
    provider_id: str,
    intent: str = "auto",
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
        run_research_advisors=_run_research_advisors,
        project_facts=STATE.project_facts,
        work_checkpoints=STATE.work_checkpoints,
        run_ledgers=STATE.run_ledgers,
        run_traces=STATE.run_traces,
        managed_outputs=STATE.managed_outputs,
        knowledge_store=STATE.knowledge_store,
        is_git_repository=is_git_repository,
        review_fix_turns=REVIEW_FIX_TURNS,
        review_log_lines=REVIEW_LOG_LINES,
        ghost_learning_provider_factory=getattr(STATE, "ghost_learning_provider_factory", None),
        ghost_router_provider_factory=getattr(STATE, "ghost_router_provider_factory", None),
    )
    try:
        runner.run(TaskRequest(
            session_id=session_id,
            project=project,
            task=task,
            max_turns=max_turns,
            continue_task=continue_task,
            provider_id=provider_id,
            intent=intent,
            run_id=run_id,
        ))
    finally:
        if _should_wait_for_local_ghost_sleep(STATE.state_home):
            STATE.wait_for_ghost_sleep()
        STATE.kick_self_repair()


def _submit_task(
    session_id: str,
    project: str | None,
    task: str,
    max_turns: int,
    continue_task: bool,
    provider_id: str,
    intent: str = "auto",
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
            intent,
            reserved.run_id,
        )
    except Exception:
        STATE.release_run(reserved.run_id)
        raise
    return reserved.run_id


def _query_list(query: dict[str, list[str]], key: str) -> list[str]:
    values: list[str] = []
    for raw in query.get(key, []):
        for item in str(raw or "").split(","):
            text = item.strip()
            if text and text not in values:
                values.append(text)
    return values


def _query_int(
    query: dict[str, list[str]],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int((query.get(key) or [default])[0])
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _query_bool(query: dict[str, list[str]], key: str, default: bool) -> bool:
    value = str((query.get(key) or [""])[0]).strip().lower()
    if not value:
        return default
    return value not in {"0", "false", "no", "off"}


def _query_value(query: dict[str, list[str]], key: str) -> str:
    return str((query.get(key) or [""])[0]).strip()


def _research_unconfigured_response() -> tuple[int, dict]:
    return 404, {"ok": False, "error": "Research is not configured"}


def _research_graph_response(query: dict[str, list[str]]) -> tuple[int, dict]:
    if STATE.knowledge_store is None:
        return _research_unconfigured_response()
    focus_ids = _query_list(query, "focus")
    synthesis_id = _query_value(query, "synthesis_id")
    if synthesis_id and synthesis_id not in focus_ids:
        focus_ids.insert(0, synthesis_id)
    graph = UnifiedResearchGraphBuilder(STATE.knowledge_store).build_for_session(
        _query_value(query, "session_id"),
        focus_ids=tuple(focus_ids),
        depth=_query_int(query, "depth", 1, 1, 3),
        node_limit=_query_int(query, "limit", 96, 8, 200),
        edge_limit=_query_int(query, "edge_limit", 192, 8, 400),
        counterpoints=tuple(_query_list(query, "counterpoint")[:8]),
    )
    return 200, {"ok": True, "graph": graph.to_dict()}


def _research_concept_graph_response(query: dict[str, list[str]]) -> tuple[int, dict]:
    if STATE.knowledge_store is None:
        return _research_unconfigured_response()
    graph = ConceptGraphBuilder(STATE.knowledge_store).build_for_session(
        _query_value(query, "session_id"),
        node_limit=_query_int(query, "limit", 64, 8, 200),
        edge_limit=_query_int(query, "edge_limit", 128, 8, 400),
    )
    return 200, {"ok": True, "graph": graph.to_dict()}


def _research_note_response(query: dict[str, list[str]]) -> tuple[int, dict]:
    note_id = _query_value(query, "id")
    if not note_id:
        return 400, {"ok": False, "error": "id required"}
    if STATE.knowledge_store is None:
        return _research_unconfigured_response()
    note = STATE.knowledge_store.read_note(note_id)
    if note is None:
        return 404, {"ok": False, "error": "note not found"}
    row = STATE.knowledge_store.index.get(note.id) or {}
    return 200, {
        "ok": True,
        "note": {
            "id": note.id,
            "type": note.type,
            "title": note.title,
            "body": note.body,
            "sources": note.sources,
            "tags": note.tags,
            "status": note.status,
            "path": str(row.get("path") or ""),
            "updated": note.updated,
        },
    }


def _research_restore_response(body: dict) -> tuple[int, dict]:
    run_id = str(body.get("run_id") or "").strip()
    if not run_id:
        return 400, {"ok": False, "error": "run_id required"}
    payload = STATE.restore_research_changes(run_id)
    return 200 if payload.get("ok") else 409, payload


def _ghost_control_surface() -> GhostControlSurface:
    return GhostControlSurface(
        inbox=STATE.ghost_inbox,
        hebbian=STATE.ghost_hebbian,
        continuity=STATE.ghost_continuity,
        router=STATE.ghost_router,
        sleep=STATE.ghost_sleep,
        work_queue=STATE.ghost_work_queue,
        affinity=STATE.ghost_affinity,
        signals=STATE.ghost_signals,
    )


def _ghost_summary_response(query: dict[str, list[str]]) -> tuple[int, dict]:
    payload = _ghost_control_surface().summary(
        session_id=_query_value(query, "session_id"),
        project=_query_value(query, "project"),
    )
    return 200, payload


def _ghost_export_response() -> tuple[int, dict]:
    return 200, _ghost_control_surface().export_state()


def _ghost_action_response(body: dict) -> tuple[int, dict]:
    return _ghost_control_surface().dispatch_action(body)


def _run_submit_response(body: dict) -> tuple[int, dict]:
    session_id = str(body.get("session_id") or "").strip() or "default"
    project = (body.get("project") or "").strip() or None
    task = (body.get("task") or "").strip()
    continue_task = bool(body.get("continue_task"))
    provider_id = str(body.get("provider") or DEFAULT_PROVIDER_ID).strip().lower()
    intent = str(body.get("intent") or "auto").strip().lower()
    if intent not in {"auto", "chat", "research", "project", "hybrid", "planning_readonly", "readonly", "planning", "review"}:
        return 400, {"error": "invalid intent"}
    try:
        max_turns = int(body.get("max_turns") or DEFAULT_MAX_TURNS)
    except (TypeError, ValueError):
        return 400, {"error": "invalid max_turns"}
    max_turns = max(1, min(max_turns, 500))
    if not task:
        return 400, {"error": "task required"}
    if provider_id not in PROVIDER_LABELS:
        return 400, {"error": f"unsupported provider: {provider_id}"}
    if intent == "review" and not project:
        return 400, {"error": "project required for review"}
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
            intent,
        )
    except Exception as exc:
        return 500, {"error": str(exc)}
    if run_id is None:
        return 409, {"error": "busy"}
    return 200, {"ok": True, "run_id": run_id}

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

    def _send_index(self) -> None:
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        html = html.replace("__CODEY_VERSION__", __version__)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._send_index()
            return
        if url.path.startswith("/assets/"):
            asset = resolve_web_asset(url.path)
            if asset is None:
                self.send_response(404)
                self.end_headers()
            else:
                self._send_file(asset[0], asset[1])
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
        if url.path == "/api/local_provider":
            self._send_json(200, {"ok": True, "local": local_config_payload()})
            return
        if url.path == "/api/research/graph":
            status, payload = _research_graph_response(parse_qs(url.query))
            self._send_json(status, payload)
            return
        if url.path == "/api/research/concept_graph":
            status, payload = _research_concept_graph_response(parse_qs(url.query))
            self._send_json(status, payload)
            return
        if url.path == "/api/research/note":
            status, payload = _research_note_response(parse_qs(url.query))
            self._send_json(status, payload)
            return
        if url.path == "/api/ghost/summary":
            status, payload = _ghost_summary_response(parse_qs(url.query))
            self._send_json(status, payload)
            return
        if url.path == "/api/ghost/export":
            status, payload = _ghost_export_response()
            self._send_json(status, payload)
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
        if url.path == "/api/local_provider":
            base_url = str(body.get("base_url") or "").strip().rstrip("/")
            model = str(body.get("model") or "").strip()
            raw_api_key = body.get("api_key")
            api_key = str(raw_api_key).strip() if raw_api_key is not None else ""
            if not base_url:
                self._send_json(400, {"ok": False, "error": "base_url required"})
                return
            previous = load_local_config()
            probe_key = api_key if api_key else str(previous.get("api_key") or "")
            endpoint = probe_local_endpoint(base_url, api_key=probe_key)
            if endpoint is None:
                self._send_json(400, {"ok": False, "error": "could not reach an OpenAI-compatible /models endpoint"})
                return
            try:
                save_local_config(
                    endpoint.base_url,
                    model or endpoint.default_model,
                    api_key if api_key else None,
                )
            except (OSError, ValueError) as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
                return
            self._send_json(200, {"ok": True, "local": local_config_payload()})
            return
        if url.path == "/api/run":
            status, payload = _run_submit_response(body)
            self._send_json(status, payload)
            return
        if url.path == "/api/research/restore":
            status, payload = _research_restore_response(body)
            self._send_json(status, payload)
            return
        if url.path == "/api/ghost/action":
            status, payload = _ghost_action_response(body)
            self._send_json(status, payload)
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
                setup_context = _shell_continuation_setup_context(pending)
                followup_hints = _shell_followup_hints(
                    pending=pending,
                    result=result,
                )
                continuation = build_shell_approval_continuation(
                    command=command,
                    result=result,
                    post_approval_instructions=str(
                        pending.get("post_approval_instructions") or ""
                    ),
                    setup_context=setup_context,
                    followup_hints=followup_hints,
                )
                continuation_run = _submit_task(
                    session_id,
                    pending["project"],
                    continuation,
                    int(pending["max_turns"]),
                    True,
                    pending.get("provider") or DEFAULT_PROVIDER_ID,
                    "project",
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
    _start_provider_warmup()

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
