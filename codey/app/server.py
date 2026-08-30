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
    GET  /api/run_details query {session_id, run_id} → returns a bounded,
                          user-facing run explanation
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
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from codey.runtime import cancellation
from codey.providers import profile_doctor
from codey.providers import controls as provider_controls, flow as provider_flow
from codey import __version__
from codey.agents.request import DEFAULT_MAX_TURNS
from codey.agents.runner import run as agent_run
from codey.automation.browser_worker import submit as submit_browser_task
from codey.workspace.changes import (
    ChangeTracker,
    SnapshotStore,
    collect_changes,
    is_git_repository,
    restore_snapshot_changes,
)
from codey.agents.consensus import ConsensusAdvice, ConsensusResult, run_consensus, run_project_audit
from codey.agents.handoff import ConversationContext
from codey.storage.local_store import DEFAULT_STATE_HOME
from codey.ghost.affinity import GhostAffinityStore
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.continuity import GhostContinuityStore
from codey.ghost.hebbian import GhostHebbianStore
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.router import GhostRouteStore
from codey.ghost.sleep import GhostSleepStore
from codey.storage.managed_outputs import ManagedOutputStore
from codey.ghost.store import GhostSignalStore
from codey.ghost.work_queue import GhostWorkQueueStore
from codey.knowledge.concepts import ConceptGraphBuilder
from codey.knowledge.store import KnowledgeStore
from codey.knowledge.unified_graph import UnifiedResearchGraphBuilder
from codey.runtime.prompt_envelope import FailOpenPromptTrace, record_provider_send_prompt
from codey.research.advisors import EvidencePack, run_research_advisors
from codey.research.evidence_ledger import EvidenceLedgerStore
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
from codey.providers.capabilities import rank_providers
from codey.providers.diagnostics import capture_provider_failure
from codey.repairs.adapter_repair import AdapterRepairResult
from codey.repairs.self_repair import SelfRepairJob, SelfRepairSupervisor
from codey.repairs.self_repair_worker import run_self_repair_worker
from codey.workspace.facts import ProjectFactsStore
from codey.workspace.revision import WorkspaceRevisionStore
from codey.workspace.task_context import safe_verification_candidates
from codey.runs.details import load_run_details
from codey.runs.ledger import RunLedgerStore
from codey.runs.trace import RunTraceStore
from codey.runtime.effects import RuntimeOperationStore
from codey.runtime.session_log import RuntimeSessionLog
from codey.policies.shell_followup import ShellFollowupInput, render_shell_followup
from codey.workspace.setup_context import safe_setup_context
from codey.runs.work_checkpoint import WorkCheckpointStore
from codey.reviews.core import (
    ReviewResult,
    parse_review_with_repair,
    render_review_prompt,
)
from codey.reviews.impact_map import safe_review_impact_map
from codey.app.approval_registry import ApprovalRegistry
from codey.app.conversation_registry import ConversationRegistry
from codey.app.ghost_daemon import GhostSleepDaemon
from codey.app.knowledge_indexer import KnowledgeIndexer
from codey.app.provider_registry import ProviderRegistry
from codey.app.run_registry import RunRegistry, RunSnapshot, same_project
from codey.task.model import TaskSubmission
from codey.operations.task_entry import TaskRunDeps, run_task_submission
from codey.app.event_bus import EventBus, EventSubscriber
from codey.utils.text_budget import clip_middle
from codey.storage.ui_state_store import UiStateStore

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
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
SHELL_CONTINUATION_IDLE_TIMEOUT = 5.0
SHELL_CONTINUATION_IDLE_POLL = 0.02
REVIEW_TIMEOUT = 300.0
REVIEW_FIX_TURNS = 12
REVIEW_LOG_LINES = 80
CONTROL_TEACH_TIMEOUT = 300.0
PROFILE_DOCTOR_TIMEOUT = 90.0
MAX_POST_BODY_BYTES = 16 * 1024 * 1024
SSE_REPLAY_LIMIT = 512


def _profile_doctor_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    return max(0.1, min(PROFILE_DOCTOR_TIMEOUT, remaining))
MAX_CONVERSATION_STATES = 32


def _parse_sse_event_id(value: object) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _sse_replay_cursor(value: object) -> int | None:
    parsed = _parse_sse_event_id(value)
    return parsed if value is not None and parsed > 0 else None


def reviewer_candidates(writer_id: str, *, supervisor: object | None = None) -> tuple[str, ...]:
    writer = (writer_id or DEFAULT_PROVIDER_ID).strip().lower()
    if supervisor is None:
        supervisor = STATE.providers.supervisor
    candidates = tuple(
        provider_id
        for provider_id in PROVIDER_LABELS
        if provider_id != writer
        and provider_id != "local"
        and supervisor.is_available(provider_id)
    )
    return rank_providers(candidates, mode="review")


def review_label(provider_id: str) -> str:
    return PROVIDER_LABELS.get(provider_id, provider_id)


def provider_availability() -> dict[str, bool]:
    return provider_availability_from_statuses(provider_tab_availability())


def provider_availability_from_statuses(statuses: dict[str, bool]) -> dict[str, bool]:
    supervisor = globals()["STATE"].providers.supervisor
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
    trace_recorder: object | None = None,
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
            trace_recorder=trace_recorder,
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
    trace_recorder: object | None = None,
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
        trace_recorder=trace_recorder,
    )


def _run_project_audit(
    *,
    project: str | Path,
    selected_provider=None,
    selected_provider_id: str,
    task: str,
    context: str = "",
    trace_recorder: object | None = None,
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
        trace_recorder=trace_recorder,
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
        # Run through the shared process-tree owner so Stop also terminates
        # children of the approved command instead of orphaning them.
        with cancellation.scope(STATE.run_registry.stop_flag):
            proc = cancellation.run_process(
                command,
                cwd=cwd,
                timeout=SHELL_TIMEOUT,
                shell=True,
            )
    except cancellation.TaskCancelled:
        return {
            "ok": False,
            "error": "command stopped",
            "exit_code": None,
            "output": "",
        }
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


class AppContext:
    def __init__(self, state_home: str | Path | None = None) -> None:
        self._ephemeral_runtime_home = tempfile.TemporaryDirectory() if state_home is None else None
        self.state_home = Path(state_home) if state_home else None
        runtime_state_home = (
            self.state_home
            if self.state_home is not None
            else Path(self._ephemeral_runtime_home.name)
        )
        self.lock = threading.Lock()
        self.event_bus = EventBus(replay_limit=SSE_REPLAY_LIMIT)
        self.run_registry = RunRegistry()
        self.approvals = ApprovalRegistry()
        self.research_changes: dict[str, object] = {}
        self.change_trackers: dict[str, ChangeTracker] = {}
        self.conversation_registry = ConversationRegistry(
            state_home,
            max_states=MAX_CONVERSATION_STATES,
        )
        self.ui_state_store_lock = threading.Lock()
        self.providers = ProviderRegistry(state_home)
        self.project_facts = (
            ProjectFactsStore(state_home) if state_home else ProjectFactsStore()
        )
        resolved_state_home = Path(state_home).expanduser().resolve() if state_home else None
        self.knowledge_store = (
            KnowledgeStore(Path(state_home) / "vault")
            if resolved_state_home == DEFAULT_STATE_HOME.expanduser().resolve()
            else None
        )
        self.knowledge_indexer = KnowledgeIndexer(
            lock=self.lock,
            store=lambda: self.knowledge_store,
        )
        self.work_checkpoints = (
            WorkCheckpointStore(state_home) if state_home else WorkCheckpointStore()
        )
        self.workspace_revisions = WorkspaceRevisionStore(runtime_state_home)
        self.run_ledgers = RunLedgerStore(state_home) if state_home else None
        self.run_traces = RunTraceStore(state_home) if state_home else None
        self.runtime_log = RuntimeSessionLog(runtime_state_home)
        self.runtime_operations = RuntimeOperationStore(self.runtime_log)
        self.evidence_ledgers = EvidenceLedgerStore(state_home) if state_home else None
        self.managed_outputs = ManagedOutputStore(state_home) if state_home else None
        self.ghost_inbox = GhostInboxStore(state_home) if state_home else None
        self.ghost_hebbian = GhostHebbianStore(state_home) if state_home else None
        self.ghost_continuity = GhostContinuityStore(state_home) if state_home else None
        self.ghost_router = GhostRouteStore(state_home) if state_home else None
        self.ghost_sleep = GhostSleepStore(state_home) if state_home else None
        self.ghost_work_queue = GhostWorkQueueStore(state_home) if state_home else None
        self.ghost_affinity = GhostAffinityStore(state_home) if state_home else None
        self.ghost_signals = GhostSignalStore(state_home) if state_home else None
        self.ghost_sleep_daemon = GhostSleepDaemon(
            lock=self.lock,
            is_busy=self.is_busy,
            stop_requested=lambda: self.run_registry.stop_flag.is_set(),
            run_once=self._run_ghost_sleep_once,
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
        run_id: str = "",
        abort_if_stopped: bool = False,
    ) -> RunSnapshot | None:
        """Atomically reserve the single browser task slot.

        With ``abort_if_stopped`` a pending user Stop wins inside the same
        lock: the slot stays free, the flag is left set, and no reservation
        happens. This closes the check-then-act race where an external
        stop-flag peek could not see a Stop landing between the peek and
        this reserve.
        """
        return self.run_registry.reserve(
            session_id=session_id,
            project=project,
            task=task,
            provider_id=provider_id,
            run_id=run_id,
            abort_if_stopped=abort_if_stopped,
        )

    def active_run_for(
        self,
        *,
        session_id: str = "",
        project: str | None = None,
    ) -> RunSnapshot | None:
        """Return the active run when it matches the given scope.

        Project scope compares resolved paths, so an active run started with
        a differently-spelled but identical directory still matches.
        """
        return self.run_registry.active_for(session_id=session_id, project=project)

    def current_run(self) -> RunSnapshot | None:
        return self.run_registry.current()

    def is_busy(self) -> bool:
        return self.run_registry.is_busy()

    def run_status(self) -> str:
        return self.run_registry.status()

    def replace_reserved_run(self, current_run_id: str, replacement: RunSnapshot) -> bool:
        return self.run_registry.replace_active(current_run_id, replacement)

    def has_active_run_for_project(self, project_key: str) -> bool:
        """True when the active run writes inside the given project."""
        return self.active_run_for(project=project_key) is not None

    @staticmethod
    def _same_project(left: str, right: str) -> bool:
        return same_project(left, right)

    def start_run(self, run_id: str) -> bool:
        return self.run_registry.start(run_id)

    def set_run_status(self, status: str) -> None:
        self.run_registry.set_status(status)

    def switch_run_provider(self, run_id: str, provider_id: str) -> bool:
        return self.run_registry.switch_provider(run_id, provider_id)

    def release_run(self, run_id: str) -> None:
        self.run_registry.release(run_id)

    def finish_run(self, run_id: str, event: dict) -> bool:
        payload = self.run_registry.finish(run_id, event)
        if payload is None:
            return False
        self.emit(payload)
        return True

    def record_shell_result(self, event: dict) -> None:
        payload = self.run_registry.record_shell_result(event)
        self.emit(payload)

    def expire_pending_shell_approvals(self) -> None:
        """Expire every pending shell approval (task stop path).

        A stale Allow card must never execute a command -- and resume work
        -- after the user pressed stop, so stop clears the map and emits a
        denied shell_result for each expired approval under the same lock.
        """

        with self.lock:
            events = self.approvals.expire_shell_results()
        for event in events:
            self.record_shell_result(event)

    def add_pending_shell_approval(self, approval_id: str, pending: dict) -> None:
        with self.lock:
            self.approvals.add_shell(approval_id, pending)

    def pop_pending_shell_approval(self, approval_id: str) -> dict | None:
        with self.lock:
            return self.approvals.pop_shell(approval_id)

    def pending_shell_approvals(self) -> dict[str, dict]:
        with self.lock:
            return self.approvals.shell_snapshot()

    def add_pending_teach(self, teach_id: str, pending: dict) -> None:
        with self.lock:
            self.approvals.add_teach(teach_id, pending)

    def pop_pending_teach(self, teach_id: str) -> dict | None:
        with self.lock:
            return self.approvals.pop_teach(teach_id)

    def resume_pending_teach(self, teach_id: str) -> bool:
        with self.lock:
            return self.approvals.resume_teach(teach_id)

    def cancel_pending_teach(self) -> None:
        with self.lock:
            self.approvals.cancel_teach()

    def pending_teach_requests(self) -> dict[str, dict]:
        with self.lock:
            return self.approvals.teach_snapshot()

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
        self.knowledge_indexer.schedule()

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
        return self.ghost_sleep_daemon.kick(payload)

    def _run_ghost_sleep_once(self, payload: dict[str, object]) -> None:
        sleep = getattr(self, "ghost_sleep", None)
        if sleep is None:
            return
        sleep.run_once(
            inbox_store=getattr(self, "ghost_inbox", None),
            hebbian_store=getattr(self, "ghost_hebbian", None),
            continuity_store=getattr(self, "ghost_continuity", None),
            work_queue_store=getattr(self, "ghost_work_queue", None),
            affinity_store=getattr(self, "ghost_affinity", None),
            router_store=getattr(self, "ghost_router", None),
            knowledge_store=getattr(self, "knowledge_store", None),
            run_projection=payload.get("run_projection"),
            trigger=str(payload.get("trigger") or "post_turn"),
            run_id=str(payload.get("run_id") or ""),
            session_id=str(payload.get("session_id") or ""),
            project=str(payload.get("project") or ""),
            should_cancel=self.ghost_sleep_daemon.should_cancel_current,
        )

    def wait_for_ghost_sleep(self, timeout: float | None = None) -> bool:
        return self.ghost_sleep_daemon.wait(timeout)

    def run_state_payload(self) -> dict:
        with self.lock:
            research_restore_runs = tuple(sorted(self.research_changes))
        return self.run_registry.payload(
            pending_event=self._pending_ui_event,
            research_restore_runs=research_restore_runs,
        )

    def _pending_ui_event(self, active: RunSnapshot | None) -> dict | None:
        with self.lock:
            return self.approvals.pending_ui_event(active)

    def emit(self, event: dict) -> None:
        with self.lock:
            payload = dict(event)
            active = self.run_registry.current()
            if (
                active is not None
                and payload.get("type") in RUN_EVENT_TYPES
                and not payload.get("run_id")
                and payload.get("session_id") in (None, active.session_id)
            ):
                payload["run_id"] = active.run_id
                payload.setdefault("session_id", active.session_id)
        self.event_bus.emit(payload)

    def subscribe(self) -> EventSubscriber:
        return self.event_bus.subscribe()

    def unsubscribe(self, sub: EventSubscriber) -> None:
        self.event_bus.unsubscribe(sub)

    def replay_events_after(
        self,
        last_event_id: int,
        *,
        max_event_id: int | None = None,
    ) -> list[tuple[int, dict]]:
        return self.event_bus.replay_events_after(
            last_event_id,
            max_event_id=max_event_id,
        )

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
            if self.is_busy() or self._self_repair_running:
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
        return self.providers.self_repair_candidates(
            broken_provider_id,
            ordered=self.provider_failover_order(),
        )

    def provider_failover_order(self) -> tuple[str, ...]:
        return self.providers.failover_order(provider_tab_availability)

    def conversation_for(self, session_id: str) -> ConversationContext:
        return self.conversation_registry.for_session(session_id)

    def forget_conversation(self, session_id: str) -> None:
        self.conversation_registry.forget(session_id)
        with self.lock:
            self.providers.forget_session(session_id)
        self.run_registry.clear_session_outputs(session_id)
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
        operations = getattr(self, "runtime_operations", None)
        if operations is not None:
            try:
                operations.delete_session(session_id)
            except Exception:
                pass

    def provider_session_changed(self, provider_id: str, session_id: str) -> bool:
        with self.lock:
            return self.providers.session_changed(provider_id, session_id)

    def set_provider_session(self, provider_id: str, session_id: str | None) -> None:
        with self.lock:
            self.providers.set_session(provider_id, session_id)

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
                active = self.current_run()
                run_id = active.run_id if active is not None and active.session_id == request.session_id else ""
                pending["ui_event"] = {
                    "type": "teach_request",
                    "run_id": run_id,
                    "session_id": request.session_id,
                    "id": teach_id,
                    "text": request.message,
                }
                self.approvals.add_teach(teach_id, pending)
            self.emit(pending["ui_event"])
            if not pending["event"].wait(CONTROL_TEACH_TIMEOUT):
                self.pop_pending_teach(teach_id)
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
                self.pop_pending_teach(teach_id)

    def handle_profile_doctor(
        self,
        request: profile_doctor.ProfileDoctorRequest,
    ) -> str | None:
        """Try healthy sibling tabs within one bounded recovery deadline."""
        cancellation.check()
        deadline = time.monotonic() + PROFILE_DOCTOR_TIMEOUT
        for provider_id in reviewer_candidates(
            request.provider_id,
            supervisor=self.providers.supervisor,
        )[:3]:
            cancellation.check()
            if not self.providers.supervisor.is_available(provider_id):
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
        for provider_id in reviewer_candidates(
            request.provider_id,
            supervisor=self.providers.supervisor,
        )[:3]:
            cancellation.check()
            if not self.providers.supervisor.is_available(provider_id):
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


STATE = AppContext(DEFAULT_STATE_HOME)
STATE.providers.ghost_learning_provider_factory = connect_fresh_provider_tab
STATE.providers.ghost_router_provider_factory = connect_fresh_provider_tab
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
    deps = TaskRunDeps(
        state=STATE,
        agent_run=agent_run,
        collect_changes=collect_changes,
        run_review=_run_review,
        capture_provider_failure=capture_provider_failure,
        run_consensus=_run_consensus,
        run_project_audit=_run_project_audit,
        run_research_advisors=_run_research_advisors,
        project_facts=STATE.project_facts,
        work_checkpoints=STATE.work_checkpoints,
        workspace_revisions=STATE.workspace_revisions,
        run_ledgers=STATE.run_ledgers,
        run_traces=STATE.run_traces,
        evidence_ledgers=STATE.evidence_ledgers,
        managed_outputs=STATE.managed_outputs,
        knowledge_store=STATE.knowledge_store,
        is_git_repository=is_git_repository,
        review_fix_turns=REVIEW_FIX_TURNS,
        review_log_lines=REVIEW_LOG_LINES,
        ghost_learning_provider_factory=STATE.providers.ghost_learning_provider_factory,
        ghost_router_provider_factory=STATE.providers.ghost_router_provider_factory,
    )
    try:
        run_task_submission(
            deps,
            TaskSubmission(
                session_id=session_id,
                project=project,
                task=task,
                max_turns=max_turns,
                continue_task=continue_task,
                provider_id=provider_id,
                intent=intent,
                run_id=run_id,
            )
        )
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
    *,
    abort_if_stopped: bool = False,
) -> str | None:
    reserved = STATE.reserve_run(
        session_id=session_id,
        project=project,
        task=task,
        provider_id=provider_id,
        abort_if_stopped=abort_if_stopped,
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


def _submit_task_after_slot_release(
    session_id: str,
    project: str | None,
    task: str,
    max_turns: int,
    continue_task: bool,
    provider_id: str,
    intent: str = "auto",
    *,
    previous_run_id: str = "",
    timeout: float = SHELL_CONTINUATION_IDLE_TIMEOUT,
) -> str | None:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        # Fast path; the authoritative guard is the atomic
        # abort_if_stopped reservation below, which closes the race where a
        # Stop lands between this peek and the reserve.
        if STATE.run_registry.stop_flag.is_set():
            return None
        active = STATE.current_run()
        if active is not None and previous_run_id and active.run_id != previous_run_id:
            return None
        run_id = _submit_task(
            session_id,
            project,
            task,
            max_turns,
            continue_task,
            provider_id,
            intent,
            abort_if_stopped=True,
        )
        if run_id is not None:
            return run_id
        active = STATE.current_run()
        if active is not None and previous_run_id and active.run_id != previous_run_id:
            return None
        if time.monotonic() >= deadline:
            return None
        time.sleep(SHELL_CONTINUATION_IDLE_POLL)


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


def _run_details_response(query: dict[str, list[str]]) -> tuple[int, dict]:
    session_id = str((query.get("session_id") or [""])[0] or "").strip()
    run_id = str((query.get("run_id") or [""])[0] or "").strip()
    if not session_id or not run_id:
        return 400, {"ok": False, "error": "session_id and run_id required"}
    summary = load_run_details(
        run_ledgers=STATE.run_ledgers,
        run_traces=STATE.run_traces,
        runtime_operations=STATE.runtime_operations,
        session_id=session_id,
        run_id=run_id,
    )
    return 200, {
        "ok": True,
        "available": summary.available,
        "details": summary.to_jsonable(),
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

    def _loopback_allowed_hosts(self) -> set[str]:
        """Host headers this server accepts.

        The UI is a local tool. Pinning the accepted Host header closes the
        DNS-rebinding door: a browser pointed at an attacker-controlled
        domain that resolves to 127.0.0.1 sends that foreign domain as Host
        and is rejected before any handler logic runs. A non-loopback bind
        (explicit LAN serving) additionally allows the bound address itself.
        """

        try:
            port = self.server.server_address[1]
            bind_ip = str(self.server.server_address[0] or "")
        except Exception:
            return set()
        hosts = {
            f"127.0.0.1:{port}",
            f"localhost:{port}",
            f"[::1]:{port}",
            "127.0.0.1",
            "localhost",
            "[::1]",
        }
        if bind_ip and bind_ip not in {"", "0.0.0.0", "::"}:
            hosts.add(f"{bind_ip}:{port}")
            hosts.add(bind_ip)
        return hosts

    def _request_origin_allowed(self) -> bool:
        host_header = str(self.headers.get("Host") or "").strip().lower()
        if host_header not in self._loopback_allowed_hosts():
            return False
        origin = str(self.headers.get("Origin") or "").strip()
        if not origin:
            return True
        return origin.rstrip("/").lower() in self._request_allowed_origins()

    def _request_allowed_origins(self) -> set[str]:
        try:
            port = self.server.server_address[1]
            bind_ip = str(self.server.server_address[0] or "")
        except Exception:
            return set()
        origins = {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
            f"http://[::1]:{port}",
        }
        if bind_ip and bind_ip not in {"", "0.0.0.0", "::", "127.0.0.1", "::1"}:
            host = f"[{bind_ip}]" if ":" in bind_ip and not bind_ip.startswith("[") else bind_ip
            origins.add(f"http://{host}:{port}")
        return {item.lower() for item in origins}

    def _deny_foreign_origin(self) -> None:
        self._send_json(403, {"error": "cross-origin request refused"})

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
        if not self._request_origin_allowed():
            self._deny_foreign_origin()
            return
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
        if url.path == "/api/run_details":
            status, payload = _run_details_response(parse_qs(url.query))
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
        if not self._request_origin_allowed():
            self._deny_foreign_origin()
            return
        url = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "invalid content length"})
            return
        if length < 0:
            self._send_json(400, {"error": "invalid content length"})
            return
        if length > MAX_POST_BODY_BYTES:
            self._send_json(413, {"error": "request body too large"})
            return
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
            previous_base_url = str(previous.get("base_url") or "").rstrip("/")
            # Never replay a stored credential against a different base_url:
            # a rebinding/XSS page could otherwise exfiltrate the saved key
            # to an attacker endpoint in one request. Changing the target
            # requires explicitly supplying the key for that target. An
            # orphaned legacy key without its original base_url is not
            # portable and is cleared unless the user supplies a new key.
            probe_key = api_key
            same_target = bool(previous_base_url) and base_url == previous_base_url
            target_changed = bool(previous_base_url) and base_url != previous_base_url
            if not probe_key and same_target:
                probe_key = str(previous.get("api_key") or "")
            if not api_key and target_changed:
                self._send_json(400, {
                    "ok": False,
                    "error": "api_key required when base_url changes",
                })
                return
            endpoint = probe_local_endpoint(base_url, api_key=probe_key)
            if endpoint is None:
                self._send_json(400, {"ok": False, "error": "could not reach an OpenAI-compatible /models endpoint"})
                return
            try:
                save_local_config(
                    endpoint.base_url,
                    model or endpoint.default_model,
                    None if same_target and not api_key else api_key,
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
            # Restoring files while a task is writing to the same project
            # would corrupt the run's change set.
            if STATE.has_active_run_for_project(key):
                self._send_json(409, {"ok": False, "error": "run in progress"})
                return
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
            pending = STATE.pop_pending_shell_approval(approval_id)
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
            continuation_stopped = False
            if pending.get("continue_after"):
                if STATE.run_registry.stop_flag.is_set():
                    # The user stopped while the approved command ran; do not
                    # resurrect the task through the continuation slot.
                    continuation_stopped = True
                else:
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
                    active = STATE.current_run()
                    active_provider = (
                        active.provider_id
                        if active is not None
                        and active.run_id == str(pending.get("run_id") or "")
                        and active.session_id == session_id
                        else ""
                    )
                    provider_id = active_provider or pending.get("provider") or DEFAULT_PROVIDER_ID
                    continuation_run = _submit_task_after_slot_release(
                        session_id,
                        pending["project"],
                        continuation,
                        int(pending["max_turns"]),
                        True,
                        provider_id,
                        "project",
                        previous_run_id=str(pending.get("run_id") or ""),
                    )
                    continued = continuation_run is not None
                    continuation_stopped = not continued and STATE.run_registry.stop_flag.is_set()
            self._send_json(200, {
                "ok": True,
                "approved": True,
                "continued": continued,
                "stopped": continuation_stopped,
                "result": result,
                "event": event,
            })
            return
        if url.path == "/api/teach/resume":
            teach_id = str(body.get("id") or "").strip()
            if not STATE.resume_pending_teach(teach_id):
                self._send_json(404, {"error": "pause not found"})
                return
            self._send_json(200, {"ok": True})
            return
        if url.path == "/api/new_chat":
            session_id = str(body.get("session_id") or "").strip()
            if not session_id:
                self._send_json(400, {"ok": False, "error": "session_id required"})
                return
            # Forgetting a conversation mid-run would desynchronize the live
            # task's snapshot from its storage.
            if STATE.active_run_for(session_id=session_id) is not None:
                self._send_json(409, {"ok": False, "error": "run in progress"})
                return
            STATE.forget_conversation(session_id)
            self._send_json(200, {"ok": True})
            return
        if url.path == "/api/stop":
            STATE.run_registry.stop_flag.set()
            STATE.cancel_pending_teach()
            STATE.expire_pending_shell_approvals()
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
        replay_cursor = _sse_replay_cursor(self.headers.get("Last-Event-ID"))
        q = STATE.subscribe()
        try:
            if not self._write_sse_event({"type": "hello", "status": STATE.run_status()}):
                return
            if replay_cursor is not None:
                for event_id, replay in STATE.replay_events_after(
                    replay_cursor,
                    max_event_id=q.replay_cutoff,
                ):
                    if not self._write_sse_event(replay, event_id=event_id):
                        return
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
                if not self._write_sse_event(ev, event_id=getattr(ev, "event_id", 0)):
                    break
        finally:
            STATE.unsubscribe(q)

    def _write_sse_event(self, event: dict, *, event_id: int = 0) -> bool:
        data = json.dumps(dict(event), ensure_ascii=False)
        prefix = f"id: {event_id}\n" if event_id > 0 else ""
        try:
            self.wfile.write(f"{prefix}data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()
            return True
        except Exception:
            return False


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
