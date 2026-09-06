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
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlparse

from codey.runtime import cancellation
from codey.providers import profile_doctor
from codey.providers import controls as provider_controls, flow as provider_flow
from codey import __version__
from codey.agents.runner import run as agent_run
from codey.app import api as app_api
from codey.app import services as app_services
from codey.app.http_plumbing import (
    WEB_DIR,
    request_origin_allowed,
    resolve_web_asset,
    send_file,
    send_index,
    send_json,
    sse_replay_cursor,
    write_sse_event,
)
from codey.automation.browser_worker import submit as submit_browser_task
from codey.workspace.changes import (
    ChangeTracker,
    SnapshotStore,
    collect_changes,
    is_git_repository,
)
from codey.agents.handoff import ConversationContext
from codey.storage.local_store import DEFAULT_STATE_HOME
from codey.ghost.affinity import GhostAffinityStore
from codey.ghost.continuity import GhostContinuityStore
from codey.ghost.hebbian import GhostHebbianStore
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.router import GhostRouteStore
from codey.ghost.sleep import GhostSleepStore
from codey.storage.managed_outputs import ManagedOutputStore
from codey.ghost.store import GhostSignalStore
from codey.ghost.work_queue import GhostWorkQueueStore
from codey.knowledge.store import KnowledgeStore
from codey.research.evidence_ledger import EvidenceLedgerStore
from codey.providers import (
    DEFAULT_PROVIDER_ID,
    borrow_open_provider,
    connect_fresh_provider_tab,
    provider_tab_availability,
    connect_provider,
)
from codey.providers.diagnostics import capture_provider_failure
from codey.repairs.adapter_repair import AdapterRepairResult
from codey.repairs.self_repair import SelfRepairJob, SelfRepairSupervisor
from codey.repairs.self_repair_worker import run_self_repair_worker
from codey.workspace.facts import ProjectFactsStore
from codey.workspace.revision import WorkspaceRevisionStore
from codey.runs.ledger import RunLedgerStore
from codey.runs.trace import RunTraceStore
from codey.runtime.effect_records import RuntimeEffectStore
from codey.runtime.mutation_line import RuntimeMutationLine
from codey.runtime.operation_state import RuntimeOperationStore
from codey.runtime.session_log import RuntimeSessionLog
from codey.runtime.tool_result_delivery import ToolResultDeliveryStore
from codey.runs.work_checkpoint import WorkCheckpointStore
from codey.app.approval_registry import ApprovalRegistry
from codey.app.conversation_registry import ConversationRegistry
from codey.app.ghost_daemon import GhostSleepDaemon
from codey.app.knowledge_indexer import KnowledgeIndexer
from codey.app.provider_registry import ProviderRegistry
from codey.app.run_registry import RunRegistry, RunSnapshot, same_project
from codey.task.model import TaskSubmission
from codey.operations.task_entry import TaskRunDeps, run_task_submission
from codey.app.event_bus import EventBus, EventSubscriber
from codey.storage.ui_state_store import UiStateStore

FOLDER_DIALOG_LOCK = threading.Lock()
SHELL_CONTINUATION_IDLE_TIMEOUT = 5.0
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
        self._knowledge_store_enabled = resolved_state_home == DEFAULT_STATE_HOME.expanduser().resolve()
        self._knowledge_store: object | None = None
        self._knowledge_root = Path(state_home) / "vault" if self._knowledge_store_enabled else None
        self.knowledge_indexer = KnowledgeIndexer(
            lock=self.lock,
            store=lambda: self._knowledge_store,
        )
        self.work_checkpoints = (
            WorkCheckpointStore(state_home) if state_home else WorkCheckpointStore()
        )
        self.workspace_revisions = WorkspaceRevisionStore(runtime_state_home)
        self.run_ledgers = RunLedgerStore(state_home) if state_home else None
        self.run_traces = RunTraceStore(state_home) if state_home else None
        self.runtime_log = RuntimeSessionLog(runtime_state_home)
        self.runtime_mutations = RuntimeMutationLine(self.runtime_log)
        self.runtime_operations = RuntimeOperationStore(self.runtime_log)
        self.runtime_effects = RuntimeEffectStore(self.runtime_log)
        self.tool_result_delivery = ToolResultDeliveryStore(self.runtime_log)
        self.evidence_ledgers = EvidenceLedgerStore(state_home) if state_home else None
        self.managed_outputs = ManagedOutputStore(state_home) if state_home else None
        self._ghost_inbox: GhostInboxStore | None = None
        self._ghost_hebbian: GhostHebbianStore | None = None
        self._ghost_continuity: GhostContinuityStore | None = None
        self._ghost_router: GhostRouteStore | None = None
        self._ghost_sleep: GhostSleepStore | None = None
        self._ghost_work_queue: GhostWorkQueueStore | None = None
        self._ghost_affinity: GhostAffinityStore | None = None
        self._ghost_signals: GhostSignalStore | None = None
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

    def _ghost_store(
        self,
        attr_name: str,
        factory: Callable[[Path], object],
    ) -> object | None:
        if self.state_home is None:
            return None
        with self.lock:
            store = getattr(self, attr_name)
            if store is None:
                store = factory(self.state_home)
                setattr(self, attr_name, store)
            return store

    @property
    def ghost_inbox(self) -> GhostInboxStore | None:
        return cast(
            GhostInboxStore | None,
            self._ghost_store("_ghost_inbox", GhostInboxStore),
        )

    @ghost_inbox.setter
    def ghost_inbox(self, value: GhostInboxStore | None) -> None:
        self._ghost_inbox = value

    @property
    def ghost_hebbian(self) -> GhostHebbianStore | None:
        return cast(
            GhostHebbianStore | None,
            self._ghost_store("_ghost_hebbian", GhostHebbianStore),
        )

    @ghost_hebbian.setter
    def ghost_hebbian(self, value: GhostHebbianStore | None) -> None:
        self._ghost_hebbian = value

    @property
    def ghost_continuity(self) -> GhostContinuityStore | None:
        return cast(
            GhostContinuityStore | None,
            self._ghost_store("_ghost_continuity", GhostContinuityStore),
        )

    @ghost_continuity.setter
    def ghost_continuity(self, value: GhostContinuityStore | None) -> None:
        self._ghost_continuity = value

    @property
    def ghost_router(self) -> GhostRouteStore | None:
        return cast(
            GhostRouteStore | None,
            self._ghost_store("_ghost_router", GhostRouteStore),
        )

    @ghost_router.setter
    def ghost_router(self, value: GhostRouteStore | None) -> None:
        self._ghost_router = value

    @property
    def ghost_sleep(self) -> GhostSleepStore | None:
        return cast(
            GhostSleepStore | None,
            self._ghost_store("_ghost_sleep", GhostSleepStore),
        )

    @ghost_sleep.setter
    def ghost_sleep(self, value: GhostSleepStore | None) -> None:
        self._ghost_sleep = value

    @property
    def ghost_work_queue(self) -> GhostWorkQueueStore | None:
        return cast(
            GhostWorkQueueStore | None,
            self._ghost_store("_ghost_work_queue", GhostWorkQueueStore),
        )

    @ghost_work_queue.setter
    def ghost_work_queue(self, value: GhostWorkQueueStore | None) -> None:
        self._ghost_work_queue = value

    @property
    def ghost_affinity(self) -> GhostAffinityStore | None:
        return cast(
            GhostAffinityStore | None,
            self._ghost_store("_ghost_affinity", GhostAffinityStore),
        )

    @ghost_affinity.setter
    def ghost_affinity(self, value: GhostAffinityStore | None) -> None:
        self._ghost_affinity = value

    @property
    def ghost_signals(self) -> GhostSignalStore | None:
        return cast(
            GhostSignalStore | None,
            self._ghost_store("_ghost_signals", GhostSignalStore),
        )

    @ghost_signals.setter
    def ghost_signals(self, value: GhostSignalStore | None) -> None:
        self._ghost_signals = value

    @property
    def knowledge_store(self) -> object | None:
        if not self._knowledge_store_enabled:
            return self._knowledge_store
        with self.lock:
            if self._knowledge_store is None:
                assert self._knowledge_root is not None
                self._knowledge_store = KnowledgeStore(self._knowledge_root)
            return self._knowledge_store

    @knowledge_store.setter
    def knowledge_store(self, value: object | None) -> None:
        with self.lock:
            self._knowledge_store = value
            self._knowledge_store_enabled = value is not None
            if value is not None and self._knowledge_root is None and self.state_home is not None:
                self._knowledge_root = self.state_home / "vault"

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
            "providers": app_services.provider_status_update(provider_id, True),
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

    def forget_conversation(self, session_id: str) -> dict[str, str]:
        failures: dict[str, str] = {}
        self.conversation_registry.forget(session_id)
        with self.lock:
            self.providers.forget_session(session_id)
        self.run_registry.clear_session_outputs(session_id)
        for store_name, attr_name, method in (
            ("ghost_continuity", "ghost_continuity", lambda s: s.delete_scope("session", session_id=session_id)),
            ("ghost_router", "ghost_router", lambda s: s.delete_scope("session", session_id=session_id)),
            ("ghost_sleep", "ghost_sleep", lambda s: s.delete_scope("session", session_id=session_id)),
            ("ghost_work_queue", "ghost_work_queue", lambda s: s.delete_scope("session", session_id=session_id)),
            ("ghost_affinity", "ghost_affinity", lambda s: s.delete_scope("session", session_id=session_id)),
            ("run_traces", "run_traces", lambda s: s.delete_session(session_id)),
            ("runtime_log", "runtime_log", lambda s: s.delete_session(session_id)),
        ):
            target = getattr(self, attr_name, None)
            if target is not None:
                try:
                    method(target)
                except Exception as exc:
                    failures[store_name] = str(exc)
        return failures

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
        for provider_id in app_services.reviewer_candidates(
            self,
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
        for provider_id in app_services.reviewer_candidates(
            self,
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
        run_review=lambda **kwargs: app_services.run_review(STATE, **kwargs),
        capture_provider_failure=capture_provider_failure,
        run_consensus=lambda **kwargs: app_services.run_consensus(STATE, **kwargs),
        run_project_audit=lambda **kwargs: app_services.run_project_audit(STATE, **kwargs),
        run_research_advisors=lambda **kwargs: app_services.run_research_advisors(STATE, **kwargs),
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
        runtime_mutations=STATE.runtime_mutations,
        runtime_effects=STATE.runtime_effects,
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
        remaining = max(0.0, deadline - time.monotonic())
        STATE.run_registry.wait_for_slot(remaining)


def _pick_folder_response(_ctx: AppContext, body: dict) -> tuple[int, dict]:
    mode = str(body.get("mode") or "open").strip().lower()
    if mode not in {"open", "new"}:
        return 400, {"error": "invalid mode"}
    initial = str(body.get("initial") or "").strip() or None
    try:
        path = pick_folder(mode=mode, initial=initial)
    except Exception as exc:
        return 500, {"error": str(exc)}
    if not path:
        return 200, {"ok": False, "cancelled": True}
    return 200, {"ok": True, "path": path, "name": Path(path).name or path}


def _run_submit_route(_ctx: AppContext, body: dict) -> tuple[int, dict]:
    return app_api.run_submit_response(body, _submit_task)


def _shell_approval_route(ctx: AppContext, body: dict) -> tuple[int, dict]:
    return app_api.shell_approval_response(
        ctx,
        body,
        submit_task_after_slot_release=_submit_task_after_slot_release,
    )


_GET_ROUTES = {
    "/api/state": lambda ctx, _query: (200, ctx.run_state_payload()),
    "/api/ui_state": lambda ctx, _query: app_api.ui_state_response(ctx),
    "/api/providers": lambda ctx, _query: app_api.providers_response(ctx),
    "/api/local_provider": lambda _ctx, _query: app_api.local_provider_response(),
    "/api/research/graph": app_api.research_graph_response,
    "/api/research/concept_graph": app_api.research_concept_graph_response,
    "/api/research/note": app_api.research_note_response,
    "/api/run_details": app_api.run_details_response,
    "/api/ghost/summary": app_api.ghost_summary_response,
    "/api/ghost/export": lambda ctx, _query: app_api.ghost_export_response(ctx),
    "/api/changes": lambda ctx, query: app_api.changes_response(
        ctx,
        (query.get("project") or [""])[0].strip(),
    ),
}


_POST_ROUTES = {
    "/api/ui_state": app_api.save_ui_state_response,
    "/api/local_provider": lambda _ctx, body: app_api.save_local_provider_response(body),
    "/api/run": _run_submit_route,
    "/api/research/restore": app_api.research_restore_response,
    "/api/ghost/action": app_api.ghost_action_response,
    "/api/pick_folder": _pick_folder_response,
    "/api/changes": lambda ctx, body: app_api.changes_response(
        ctx,
        (body.get("project") or "").strip(),
    ),
    "/api/changes/restore": app_api.restore_changes_response,
    "/api/shell_approval": _shell_approval_route,
    "/api/teach/resume": app_api.teach_resume_response,
    "/api/new_chat": app_api.new_chat_response,
    "/api/stop": lambda ctx, _body: app_api.stop_response(ctx),
}


# ------------------------------------------------------------ http layer ---

class Handler(BaseHTTPRequestHandler):
    server_version = f"Codey/{__version__}"

    def log_message(self, fmt, *args):
        # Quiet the default access log.
        pass

    def _request_origin_allowed(self) -> bool:
        return request_origin_allowed(self)

    def _deny_foreign_origin(self) -> None:
        self._send_json(403, {"error": "cross-origin request refused"})

    def _send_json(self, status: int, payload: dict) -> None:
        send_json(self, status, payload)

    def _send_file(self, path: Path, ctype: str) -> None:
        send_file(self, path, ctype)

    def _send_index(self) -> None:
        send_index(self)

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
        route = _GET_ROUTES.get(url.path)
        if route is not None:
            status, payload = route(STATE, parse_qs(url.query))
            self._send_json(status, payload)
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

        route = _POST_ROUTES.get(url.path)
        if route is not None:
            status, payload = route(STATE, body)
            self._send_json(status, payload)
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
        replay_cursor = sse_replay_cursor(self.headers.get("Last-Event-ID"))
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
        return write_sse_event(self, event, event_id=event_id)


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
    app_services.start_provider_warmup(STATE)

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
