"""Application assembly: shared context, stores, and UI-agnostic constants.
This module owns AppContext (all local stores, registries, daemons) plus the
small constants both the HTTP server and headless runners need. server.py keeps
only HTTP/SSE routing, the global STATE, and task-runner wiring, so importing
the headless runner never builds global UI state as a side effect.
"""
from __future__ import annotations

import contextlib
import logging
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from codey.repairs.self_repair import SelfRepairSupervisor
from codey.agents.handoff import ConversationContext, ConversationSnapshot
from codey.app import event_bus
from codey.app.active_provider_registry import ActiveProviderRegistry
from codey.app.approval_registry import ApprovalRegistry
from codey.app.conversation_registry import ConversationRegistry
from codey.app.conversation_runtime import ConversationRuntime
from codey.app.durable_services import DurableAppServices
from codey.app.event_bus import EventBus, EventSubscriber
from codey.app.event_payloads import SCHEMA_VERSION
from codey.app.ghost_daemon import GhostSleepDaemon
from codey.app.ghost_services import GhostStoreAccessors, GhostStores
from codey.app.knowledge_indexer import KnowledgeIndexer
from codey.app.provider_registry import ProviderRegistry
from codey.app.run_registry import RunRegistry, RunSnapshot
from codey.app.runtime_adapters import DurableRuntimeAdapters
from codey.durable.model import (
    CommitResult,
    DurableMutation,
    TaskCheckpoint,
    thaw_json,
)
from codey.knowledge.store import KnowledgeStore
from codey.operations.task_state import TaskSubmissionStores
from codey.research.evidence_ledger import EvidenceLedgerStore
from codey.runs.ledger import RunLedgerStore
from codey.runs.trace import RunTraceStore
from codey.runs.work_checkpoint import WorkCheckpointStore
from codey.runtime.effects.shell_approval_records import ShellApprovalFact, ShellApprovalRecordStore
from codey.storage.execution_output_store import ExecutionOutputStore
from codey.storage.local_store import DEFAULT_STATE_HOME
from codey.storage.managed_outputs import ManagedOutputStore
from codey.storage.ui_state_store import UiStateStore
from codey.utils.refs import strict_exit_code
from codey.workspace.changes import ChangeTracker, SnapshotStore
from codey.workspace.facts import ProjectFactsStore
from codey.workspace.revision import WorkspaceRevisionStore

REVIEW_FIX_TURNS = 12
REVIEW_LOG_LINES = 80
SSE_REPLAY_LIMIT = 512
def _close_if_discarded(store: object) -> None:
    """Best-effort close for a double-checked loser (e.g. KnowledgeStore index)."""
    close = getattr(store, "close", None)
    if not callable(close):
        return
    with contextlib.suppress(Exception):
        close()
MAX_CONVERSATION_STATES = 32
MAX_CHANGE_TRACKERS = 32
logger = logging.getLogger(__name__)

class AppContext(GhostStoreAccessors):

    def __init__(
        self,
        state_home: str | Path | None = None,
        *,
        replay_limit: int | None = None,
        start_scheduler: bool = True,
    ) -> None:
        self._ephemeral_runtime_home = tempfile.TemporaryDirectory() if state_home is None else None
        self.state_home = Path(state_home) if state_home else None
        self.control_service: Any = None
        if self.state_home is not None:
            runtime_state_home = self.state_home
        else:
            assert self._ephemeral_runtime_home is not None
            runtime_state_home = Path(self._ephemeral_runtime_home.name)
        self.lock = threading.Lock()
        # Spawn gate: Stop's generation bump and the executor's final
        # check+Popen both hold it (never self.lock: re-entrant deadlock).
        self._shell_spawn_gate = threading.Lock()
        # Shell execution releases the spawn gate while it waits for the
        # process. Keep shutdown from closing the output store during the
        # small post-wait settlement window.
        self._shell_execution_condition = threading.Condition()
        self._shell_executions = 0
        # Read the module global at call time (not as a default value) so
        # tests can patch SSE_REPLAY_LIMIT around construction.
        self.event_bus = EventBus(
            replay_limit=SSE_REPLAY_LIMIT if replay_limit is None else replay_limit
        )
        self.run_registry = RunRegistry()
        self._active_providers = ActiveProviderRegistry()
        self._run_modes: dict[str, str] = {}
        self.approvals = ApprovalRegistry()
        self.research_changes: dict[str, object] = {}
        self._research_change_sessions: dict[str, str] = {}
        self.change_trackers: OrderedDict[str, ChangeTracker] = OrderedDict()
        self.conversation_registry = ConversationRegistry(
            state_home,
            max_states=MAX_CONVERSATION_STATES,
            load_snapshot=self._load_durable_conversation_snapshot,
            save_snapshot=self._save_durable_conversation_snapshot,
        )
        self.ui_state_store_lock = threading.Lock()
        self.providers = ProviderRegistry(state_home, emit=self.emit)
        self.connection_status = self.providers.connection_status
        self.project_facts = (
            ProjectFactsStore(state_home) if state_home else ProjectFactsStore()
        )
        resolved_state_home = Path(state_home).expanduser().resolve() if state_home else None
        self._knowledge_store_enabled = resolved_state_home == DEFAULT_STATE_HOME.expanduser().resolve()
        self._knowledge_store: KnowledgeStore | None = None
        self._knowledge_root = Path(state_home) / "vault" if state_home is not None and self._knowledge_store_enabled else None
        self.knowledge_indexer = KnowledgeIndexer(
            store=lambda: self._knowledge_store,
        )
        self.work_checkpoints = (
            WorkCheckpointStore(state_home) if state_home else WorkCheckpointStore()
        )
        self.workspace_revisions = WorkspaceRevisionStore(runtime_state_home)
        self.run_ledgers = RunLedgerStore(state_home) if state_home else None
        self.run_traces = RunTraceStore(state_home) if state_home else None
        self._durable_services = DurableAppServices.open(
            runtime_state_home / "durable.sqlite3",
            publish=self._publish_durable_commit,
            dispatch=lambda checkpoint: self._dispatch_durable_task(checkpoint),
            abort_handler=self._abort_durable_task,
            start=False,
        )
        # Keep these aliases as the application facade.  Ownership and
        # lifecycle stay in DurableAppServices; callers do not construct or
        self.durable = self._durable_services.session
        self.conversation_runtime = ConversationRuntime(
            runtime_state_home, self.emit, durable=self.durable
        )
        # Runtime lifecycle projections share one explicit durable owner.  The
        # standalone JSONL session log remains available only to isolated
        # audit/debug callers; AppContext never constructs it or recovers from
        # it.
        self.runtime_adapters = DurableRuntimeAdapters.open(self.durable)
        # These aliases keep the existing TaskState protocol surface while
        # pointing at the one explicit adapter owner above.
        self.runtime_mutations = self.runtime_adapters.mutations
        self.runtime_operations = self.runtime_adapters.operations
        self.runtime_effects = self.runtime_adapters.effects
        self.tool_result_delivery = self.runtime_adapters.delivery
        self.input_queue = self.conversation_runtime.inputs
        self.user_questions = self.conversation_runtime.questions
        self.execution_outputs = ExecutionOutputStore(runtime_state_home)
        self.shell_approval_records = ShellApprovalRecordStore(runtime_state_home)
        self.evidence_ledgers = EvidenceLedgerStore(state_home) if state_home else None
        self.managed_outputs = ManagedOutputStore(state_home) if state_home else None
        # Rebuild the UI projection for untouched approvals before the first
        # /api/state request after a process restart.  Execution still requires
        # the durable atomic claim, so this only restores display state.
        from codey.app.shell_approval_state import restore_pending_shell_approvals

        restore_pending_shell_approvals(self)
        from codey.app.shell_approval_workflow import ShellApprovalWorkflow

        self.shell_approval_workflow = ShellApprovalWorkflow(self)
        self._ghost_stores = GhostStores(state_home, self.lock)
        self.ghost_sleep_daemon = GhostSleepDaemon(
            lock=self.lock,
            is_busy=self.is_busy,
            stop_requested=lambda: any(run.stop_event.is_set() for run in self.run_registry.active_runs()),
            run_once=self._run_ghost_sleep_once,
        )
        self._self_repair: SelfRepairSupervisor | None = None
        self.snapshot_store = (
            SnapshotStore(state_home) if state_home else SnapshotStore()
        )
        self.ui_state_store = (
            UiStateStore(state_home) if state_home else UiStateStore()
        )
        self._project_writer_leases: dict[str, object] = {}
        self._close_requested = False
        self._resources_closed = False
        self._deferred_durable_events: list[dict[str, Any]] = []
        self.durable_scheduler = self._durable_services.scheduler
        if start_scheduler:
            self.durable_scheduler.start()

    def _dispatch_durable_task(self, checkpoint: object) -> bool:
        from codey.app.task_submit import dispatch_durable_task

        return dispatch_durable_task(checkpoint, state=self)

    def _abort_durable_task(self, checkpoint: TaskCheckpoint, _invocation: object) -> None:
        """Drive the durable cancel barrier into live provider and shell work."""
        run_id = str(checkpoint.task_id or "")
        if not run_id:
            return
        # Provider transports and shell waits observe this exact run-scoped
        # event through their cancellation scopes.  The durable scheduler owns
        # the checkpoint transition; this handler only interrupts live work.
        self.run_registry.request_stop(run_id)
        provider = self._active_providers.get(run_id)
        abandon = getattr(provider, "abandon_inflight", None)
        if callable(abandon):
            try:
                abandon()
            except Exception as exc:
                logger.debug("provider abort cleanup failed for %s: %s", run_id, exc)
        events: tuple[dict[str, Any], ...] = ()
        with self._shell_spawn_gate, self.lock:
            expire = getattr(self, "_expire_shell_results_locked", None)
            if callable(expire):
                events = tuple(expire(run_id=run_id))
            workflow = getattr(self, "shell_approval_workflow", None)
            cancel_queued = getattr(workflow, "cancel_queued", None)
            if callable(cancel_queued):
                cancel_queued(checkpoint.conversation_id)
        for event in events:
            self.record_shell_result(event)

    def register_active_provider(self, run_id: str, provider: object) -> None:
        """Register the raw provider transport for a live durable run."""
        if not run_id or provider is None:
            return
        self._active_providers.register(run_id, provider)

    def unregister_active_provider(self, run_id: str, provider: object | None = None) -> None:
        """Drop a run transport, preserving a newer replacement if present."""
        if not run_id:
            return
        self._active_providers.unregister(run_id, provider)

    def _publish_durable_commit(self, result: CommitResult, mutation: DurableMutation) -> None:
        if result.record is not None:
            payload = result.record.to_payload()
            # Stop/expiry may commit approval state while holding the
            # coordination lock. Queue the projection until that lock is
            # released instead of re-entering ``emit`` and deadlocking.
            if self.lock.locked():
                self._deferred_durable_events.append(payload)
            else:
                self.emit(payload)

    def _flush_deferred_durable_events(self) -> None:
        while True:
            with self.lock:
                if not self._deferred_durable_events:
                    return
                events = tuple(self._deferred_durable_events)
                self._deferred_durable_events.clear()
            for payload in events:
                self.emit(payload)

    def _load_durable_conversation_snapshot(
        self, session_id: str
    ) -> ConversationSnapshot | None:
        durable = getattr(self, "durable", None)
        if durable is None or not durable.has_conversation(session_id):
            return None
        entry = durable.latest_context_snapshot(session_id)
        return None if entry is None else ConversationSnapshot.from_payload(thaw_json(entry.payload))

    def _save_durable_conversation_snapshot(
        self, session_id: str, snapshot: ConversationSnapshot
    ) -> None:
        durable = getattr(self, "durable", None)
        if durable is None:
            return
        if not durable.has_conversation(session_id):
            durable.create_conversation(session_id)
        durable.save_context_snapshot(session_id, snapshot.to_payload())

    def acquire_project_writer(self, project: str | Path) -> bool:
        """Claim the single persistent writer for ``project`` (non-blocking).

        Returns True on success; False when another task (this process or
        another one sharing ``state_home``) already owns it. Empty projects
        need no guard and always succeed.
        """
        from codey.workspace.changes import ProjectWriteBusy

        text = str(project or "").strip()
        if not text:
            return True
        key = str(Path(text).expanduser().resolve())
        with self.lock:
            if key in self._project_writer_leases:
                return False
        try:
            lease = self.snapshot_store.acquire_writer(key, timeout_seconds=0.0)
        except ProjectWriteBusy:
            return False
        with self.lock:
            if key in self._project_writer_leases:
                with contextlib.suppress(Exception):
                    lease.release()
                return False
            self._project_writer_leases[key] = lease
            return True

    def release_project_writer(self, project: str | Path) -> None:
        text = str(project or "").strip()
        if not text:
            return
        key = str(Path(text).expanduser().resolve())
        with self.lock:
            lease = self._project_writer_leases.pop(key, None)
        if lease is not None:
            with contextlib.suppress(Exception):
                cast(Any, lease).release()

    @property

    def knowledge_store(self) -> KnowledgeStore | None:
        with self.lock:
            if not self._knowledge_store_enabled:
                return self._knowledge_store
            if self._knowledge_store is not None:
                return self._knowledge_store
            assert self._knowledge_root is not None
            root = self._knowledge_root
        # Build outside the lock: KnowledgeStore scans the vault on miss.
        fresh = KnowledgeStore(root)
        with self.lock:
            winner: KnowledgeStore | None = self._knowledge_store
            if winner is None:
                self._knowledge_store = fresh
                return fresh
        _close_if_discarded(fresh)
        return winner

    @knowledge_store.setter

    def knowledge_store(self, value: KnowledgeStore | None) -> None:
        with self.lock:
            self._knowledge_store = value
            self._knowledge_store_enabled = value is not None
            if value is not None and self._knowledge_root is None and self.state_home is not None:
                self._knowledge_root = self.state_home / "vault"

    def build_task_submission_stores(self) -> TaskSubmissionStores:
        """Capture the current resources for one task-run assembly."""
        return TaskSubmissionStores(
            project_facts=self.project_facts,
            work_checkpoints=self.work_checkpoints,
            workspace_revisions=self.workspace_revisions,
            run_ledgers=self.run_ledgers,
            run_traces=self.run_traces,
            evidence_ledgers=self.evidence_ledgers,
            managed_outputs=self.managed_outputs,
            execution_outputs=self.execution_outputs,
            knowledge_store=self.knowledge_store,
            runtime_mutations=self.runtime_adapters.mutations,
            runtime_effects=self.runtime_adapters.effects,
        )

    @property

    def task_submission_stores(self) -> TaskSubmissionStores:
        return self.build_task_submission_stores()

    @property

    def self_repair(self) -> SelfRepairSupervisor:
        from codey.repairs.adapter_repair import AdapterRepairResult
        from codey.repairs.self_repair import SelfRepairSupervisor, run_self_repair_job
        from codey.repairs.self_repair_job import SelfRepairJob

        with self.lock:
            if self._self_repair is None:
                repair_runner: Callable[[SelfRepairJob], AdapterRepairResult] | None = None
                if self.state_home is not None and self.state_home == DEFAULT_STATE_HOME:
                    def repair_runner(job: SelfRepairJob) -> AdapterRepairResult:
                        return run_self_repair_job(
                            state_home=self.state_home,
                            providers=self.providers,
                            job=job,
                        )
                self._self_repair = SelfRepairSupervisor(self.state_home, runner=repair_runner)
            return self._self_repair

    @self_repair.setter

    def self_repair(self, value: SelfRepairSupervisor | None) -> None:
        with self.lock:
            self._self_repair = value

    def load_ui_state(self) -> dict[str, Any]:
        with self.ui_state_store_lock:
            return self.ui_state_store.load()

    def save_ui_state(self, state: object, *, base_revision: int) -> dict[str, Any]:
        with self.ui_state_store_lock:
            saved = self.ui_state_store.save(state, base_revision=base_revision)
        return saved

    def change_tracker_for(
        self,
        project: str | Path,
        *,
        persistent: bool,
    ) -> ChangeTracker:
        key = str(Path(project).expanduser().resolve())
        if not persistent:
            # Read-only/Git view: never mutate a cached tracker a task may
            # hold, and never delete durable recovery data on a query path.
            # Explicit cleanup owns deletion.
            return ChangeTracker(key, None)
        with self.lock:
            tracker = self.change_trackers.get(key)
            current_persistent = tracker is not None and tracker.store is not None
            if tracker is None or not current_persistent:
                tracker = ChangeTracker(key, self.snapshot_store)
                self.change_trackers[key] = tracker
            else:
                self.change_trackers.move_to_end(key)
            self._evict_change_trackers_locked()
            return tracker

    def _evict_change_trackers_locked(self) -> None:
        """Drop oldest in-memory trackers only; durable snapshots stay on disk."""
        while len(self.change_trackers) > MAX_CHANGE_TRACKERS:
            _old_key, old_tracker = self.change_trackers.popitem(last=False)
            with contextlib.suppress(Exception):
                old_tracker.disable_persistence()

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
        """Atomically reserve the session's browser task lane.

        Different sessions may reserve independent lanes concurrently. A
        session remains serial: a second reservation for the same session is
        rejected until its active run finishes.

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

    def active_runs(self) -> tuple[RunSnapshot, ...]:
        return self.run_registry.active_runs()

    def stop_flag_for(self, run_id: str) -> threading.Event:
        return self.run_registry.stop_flag_for(run_id)

    def is_busy(self) -> bool:
        return self.run_registry.is_busy()

    def run_status(self) -> str:
        return self.run_registry.status()

    def replace_reserved_run(self, current_run_id: str, replacement: RunSnapshot) -> bool:
        return self.run_registry.replace_active(current_run_id, replacement)

    def has_active_run_for_project(self, project_key: str) -> bool:
        """True when the active run writes inside the given project."""
        return self.active_run_for(project=project_key) is not None

    def start_run(self, run_id: str) -> bool:
        return self.run_registry.start(run_id)

    def set_run_status(self, status: str) -> None:
        self.run_registry.set_status(status)

    def switch_run_provider(self, run_id: str, provider_id: str) -> bool:
        return self.run_registry.switch_provider(run_id, provider_id)

    def release_run(self, run_id: str) -> None:
        self.run_registry.release(run_id)
        self.input_queue.wake.set()

    def finish_run(self, run_id: str, event: dict[str, Any]) -> bool:
        payload = self.run_registry.finish(run_id, event)
        if payload is None:
            return False
        expired: tuple[dict[str, Any], ...] = ()
        if str(payload.get("stop_reason") or "") in {"done", "error", "max_turns", "no_progress", "stopped"}:
            with self._shell_spawn_gate, self.lock:
                expired = self._expire_shell_results_locked(
                    run_id=run_id, output="Run ended; command approval expired."
                )
        self.emit(payload)
        for expired_event in expired:
            self.record_shell_result(expired_event)
        self._flush_deferred_durable_events()
        return True

    def record_shell_result(self, event: dict[str, Any]) -> None:
        if "failure" not in event:
            status = str(event.get("status") or "")
            failure_code = {
                "output_read_error": "output.unavailable",
                "drain_timeout": "output.unavailable",
                "timeout": "execution.uncertain",
                "wait_error": "execution.uncertain",
            }.get(status)
            if failure_code:
                event = {
                    **event,
                    "failure": {
                        "schema_version": SCHEMA_VERSION,
                        "code": failure_code,
                        "category": "execution" if failure_code.startswith("execution") else "output",
                        "scope": "tool",
                        "phase": status,
                        "effect": "unknown",
                        "incident_id": f"shell:{event.get('id') or 'unknown'}:{status}",
                        "session_id": str(event.get("session_id") or ""),
                        "run_id": str(event.get("run_id") or ""),
                        "execution_id": str(event.get("execution_id") or ""),
                    },
                }
        payload = self.run_registry.record_shell_result(event)
        self.emit(payload)

    def request_stop(self, run_id: str | None = None) -> None:
        """Linearized Stop: flag, teach cancel, and approval expiry share one
        gate with the shell spawn path.

        ``stop_flag.set()`` alone never invalidates an in-flight Allow, so it
        must not happen outside the spawn gate: otherwise Stop can land
        between the executor's final check and ``Popen`` and a side-effect
        command still spawns. Every production Stop path funnels through here
        (UI stop, headless shell-reject); the executor side holds the same
        gate across final-check+Popen in ``shell_service.execute_shell_ticket``.
        """
        abort_targets: tuple[str, ...] = ()
        with self._shell_spawn_gate:
            self.run_registry.request_stop(run_id)
            with self.lock:
                # Durable cancellation is the barrier for the worker and
                # scheduler.  The in-memory stop flag only interrupts code;
                # it must never be the authority that decides whether a
                # restart may dispatch the task again.
                durable = getattr(self, "durable", None)
                if durable is not None and not self._close_requested:
                    abort_targets = (
                        (str(run_id),)
                        if run_id
                        else tuple(run.run_id for run in self.run_registry.active_runs())
                    )
                self.approvals.cancel_teach()
                if self._close_requested:
                    # Shutdown interrupts live execution but preserves durable
                    # decisions and unanswered requests for the next process.
                    self.approvals.invalidate_generation()
                    events: tuple[dict[str, Any], ...] = ()
                else:
                    target_session = self.run_registry.session_for(run_id) if run_id else ""
                    events = self._expire_shell_results_locked(run_id=run_id) if run_id else self._expire_shell_results_locked()
                    self.shell_approval_workflow.cancel_queued(target_session)
        # Route the durable cancellation through the scheduler's abort line so
        # the live provider transport and shell process receive the same
        # run-scoped cancellation signal. Do this after releasing AppContext's
        # lock: the abort handler performs shell cleanup and may publish facts.
        if abort_targets:
            for target in abort_targets:
                with contextlib.suppress(KeyError, ValueError):
                    self.durable_scheduler.abort_task(target, reason="user stopped")
        for event in events:
            self.record_shell_result(event)
        if not self._close_requested:
            target_session = self.run_registry.session_for(run_id) if run_id else ""
            if not target_session:
                owner = self.run_registry.current()
                target_session = owner.session_id if owner is not None else ""
            self.input_queue.pause(target_session)
        self._flush_deferred_durable_events()

    def expire_pending_shell_approvals(self) -> None:
        """Expire every pending shell approval (task stop path).

        A stale Allow card must never execute a command -- and resume work
        -- after the user pressed stop, so stop clears the map and emits a
        denied shell_result for each expired approval under the same lock.
        """

        with self._shell_spawn_gate, self.lock:
            events = self._expire_shell_results_locked()
        for event in events:
            self.record_shell_result(event)
        self._flush_deferred_durable_events()

    def expire_stale_shell_approvals(self, active_run_id: str) -> None:
        """Clear approval cards left behind when the user starts new work."""

        with self._shell_spawn_gate, self.lock:
            events = self._expire_shell_results_locked(
                exclude_run_id=active_run_id,
                output="A new task started; command approval expired.",
            )
        for event in events:
            self.record_shell_result(event)
        self._flush_deferred_durable_events()

    def _expire_shell_results_locked(self, **kwargs: Any) -> tuple[dict[str, Any], ...]:
        from codey.app.shell_approval_state import expire_shell_results_locked

        return expire_shell_results_locked(self, **kwargs)

    def add_pending_shell_approval(self, approval_id: str, pending: dict[str, Any]) -> None:
        fact: ShellApprovalFact | None = None
        with self.lock:
            config = self.run_registry.api_selection_for(str(pending.get("run_id") or ""))
            if config is not None and pending.get("provider") == config.connection_id:
                pending = {**pending, "_api_selection": config}
            session_id = str(pending.get("session_id") or "")
            source_run_id = str(pending.get("run_id") or "")
            existing_approval = None
            if session_id and source_run_id:
                try:
                    existing_approval = self.durable.approval(approval_id)
                except (KeyError, OSError, TypeError, ValueError):
                    existing_approval = None
            if session_id and source_run_id and existing_approval is None:
                fact = ShellApprovalFact(
                    approval_id=approval_id,
                    session_id=session_id,
                    source_run_id=source_run_id,
                    kind="requested",
                    invocation_origin="native" if pending.get("call_id") else "text",
                    command_ref=str(pending.get("command_ref") or ""),
                    command_digest=str(pending.get("command_digest") or ""),
                    cwd=str(pending.get("cwd") or "."),
                )
        # Durable commit publication can emit synchronously, so keep it
        # outside the application coordination lock.
        if fact is not None:
            self.record_shell_approval_fact(fact)
        with self.lock:
            self.approvals.add_shell(approval_id, pending)

    def record_shell_approval_fact(self, fact: ShellApprovalFact) -> bool:
        from codey.app.shell_approval_durable import record_fact

        return record_fact(self, fact)

    def _commit_durable_shell_approval(self, fact: ShellApprovalFact) -> bool:
        from codey.app.shell_approval_durable import commit_fact

        return commit_fact(self, fact)

    def _claim_pending_shell_approval_locked(
        self, approval_id: str, *, claim_execution: bool = True,
    ) -> dict[str, Any] | None:
        from codey.app.shell_approval_state import claim_pending_shell_approval_locked

        return claim_pending_shell_approval_locked(self, approval_id, claim_execution=claim_execution)

    def claim_pending_shell_approval(
        self, approval_id: str, *, claim_execution: bool = True,
    ) -> dict[str, Any] | None:
        # Durable approval CAS must not run while the application lock is held:
        # commit publication may be waiting for this lock on another worker.
        # The shell spawn gate, owned by ``shell_service``, still fences the
        # stop-versus-execution boundary.
        from codey.app.shell_approval_state import (
            _claim_decision_fact,
            _claim_execution_fact,
            _rebuild_pending_from_fact,
        )

        with self.lock:
            pending = self.approvals.shell_snapshot().get(approval_id)
        fact = None
        if pending is None:
            try:
                record = self.durable.approval(approval_id)
            except (KeyError, OSError, TypeError, ValueError):
                record = None
            if record is not None:
                from codey.app.shell_approval_state import approval_record_facts
                facts = approval_record_facts(record)
                fact = facts[-1] if facts else None
        if pending is None and fact is not None and fact.kind in {"requested", "approved"}:
            pending = _rebuild_pending_from_fact(self, fact)
        if pending is None:
            return None
        claimed = (
            _claim_execution_fact(self, pending, approval_id)
            if claim_execution
            else _claim_decision_fact(self, pending, approval_id, "denied")
        )
        if not claimed:
            return None
        with self.lock:
            self.approvals.pop_shell(approval_id)
        return pending

    def resolve_shell_approval(self, approval_id: str) -> dict[str, Any] | None:
        from codey.app.shell_approval_state import resolve_shell_approval

        return resolve_shell_approval(self, approval_id)

    def approval_generation(self) -> int:
        with self.lock:
            return self.approvals.current_generation()

    def pending_shell_approvals(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            return self.approvals.shell_snapshot()

    def add_pending_teach(self, teach_id: str, pending: dict[str, Any]) -> None:
        with self.lock:
            self.approvals.add_teach(teach_id, pending)

    def pop_pending_teach(self, teach_id: str) -> dict[str, Any] | None:
        with self.lock:
            return self.approvals.pop_teach(teach_id)

    def resume_pending_teach(self, teach_id: str) -> bool:
        with self.lock:
            return self.approvals.resume_teach(teach_id)

    def cancel_pending_teach(self) -> None:
        with self.lock:
            self.approvals.cancel_teach()

    def pending_teach_requests(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            return self.approvals.teach_snapshot()

    def record_research_changes(
        self, run_id: str, changes: object, session_id: str = ""
    ) -> None:
        with self.lock:
            self.research_changes[run_id] = changes
            owner = (session_id or "").strip()
            if not owner:
                try:
                    active = self.run_registry.current()
                except Exception:
                    active = None
                if active is not None and active.run_id == run_id:
                    owner = active.session_id
            if owner:
                self._research_change_sessions[run_id] = owner
            if len(self.research_changes) > 32:
                for key in list(self.research_changes)[:-32]:
                    self.research_changes.pop(key, None)
                    self._research_change_sessions.pop(key, None)

    def restore_research_changes(self, run_id: str) -> dict[str, Any]:
        with self.lock:
            changes = self.research_changes.get(run_id)
        if changes is None:
            return {"ok": False, "error": "research changes not found"}
        result = cast(Any, changes).restore_result()
        if self.knowledge_store is not None:
            self._schedule_knowledge_rebuild()
        if result.ok:
            with self.lock:
                self.research_changes.pop(run_id, None)
                self._research_change_sessions.pop(run_id, None)
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
                control = getattr(inbox, "control_enabled", None)
                if callable(control):
                    if not control(
                        "run_maintenance", project=project, session_id=session_id,
                    ):
                        return False
                elif not inbox.learning_enabled():
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

    def run_state_payload(self) -> dict[str, Any]:
        with self.lock:
            research_restore_runs = tuple(sorted(self.research_changes))
        payload = self.run_registry.payload(
            pending_event=self._pending_ui_event,
            research_restore_runs=research_restore_runs,
        )
        payload["shell_approvals"] = self.shell_approval_workflow.list_statuses()
        approval_work = self.shell_approval_workflow.busy_status()
        if not payload["busy"] and approval_work is not None:
            payload.update(busy=True, run_id=approval_work["source_run_id"],
                           session_id=approval_work["session_id"], run_status="running")
        return payload

    def _pending_ui_event(self, active: RunSnapshot | None) -> dict[str, Any] | None:
        with self.lock:
            return self.approvals.pending_ui_event(active)

    def emit(self, event: dict[str, Any]) -> None:
        with self.lock:
            payload = event_bus.stamp_run_scope(
                dict(event),
                self.run_registry.current(),
            )
        payload = self._event_with_run_fields(payload)
        prepared = self.conversation_runtime.prepare(self, payload)
        if prepared is None:
            return
        payload = prepared
        payload["schema_version"] = SCHEMA_VERSION
        if payload.get("type") == "tool":
            payload["ok"] = payload.get("ok") is True
            if strict_exit_code(payload.get("exit_code")) is None:
                payload.pop("exit_code", None)
        if payload.get('type') in {'tool_started', 'tool'}:
            from codey.app.tool_history import record_tool_display
            record_tool_display(self, payload)
        self.event_bus.emit(payload)
        if payload.get("type") == "task_done":
            self.input_queue.completed(str(payload.get("run_id") or ""), payload)
        self._on_event_emitted(payload)

    def emit_browser_event(self, event: dict[str, Any]) -> None:
        """Publish an observational event without changing internal facts.

        Output observation is auxiliary: it must reach SSE clients but must
        not look like a settled shell result to lifecycle hooks or stores.
        """
        with self.lock:
            payload = event_bus.stamp_run_scope(dict(event), self.run_registry.current())
        payload = self._event_with_run_fields(payload)
        payload["schema_version"] = SCHEMA_VERSION
        self.event_bus.emit(payload)

    def _on_event_emitted(self, payload: dict[str, Any]) -> None:
        """Transport hook: the bus and additional sinks receive the same scoped event."""

    def _event_with_run_fields(self, event: dict[str, Any]) -> dict[str, Any]:
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
        mode = self._run_modes.pop(run_id, "")
        if mode and not str(payload.get("mode") or "").strip():
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

    def subscribe(self) -> EventSubscriber:
        return self.event_bus.subscribe()

    def unsubscribe(self, sub: EventSubscriber) -> None:
        self.event_bus.unsubscribe(sub)

    def replay_events_after(
        self,
        last_event_id: int,
        *,
        max_event_id: int | None = None,
    ) -> list[tuple[int, dict[str, Any]]]:
        return self.event_bus.replay_events_after(
            last_event_id,
            max_event_id=max_event_id,
        )

    def get_provider(self, provider_id: str) -> Any:
        """Thin seam for tests: connect via the single provider entry point."""
        from codey.app import provider_services

        return provider_services.open_provider_session(self, provider_id)

    def provider_failover_order(self) -> tuple[str, ...]:
        """TaskState contract: open tabs first, then registry order."""
        from codey.app import provider_services

        return provider_services.provider_failover_order(self.providers)

    def conversation_for(self, session_id: str) -> ConversationContext:
        return self.conversation_registry.for_session(session_id)

    def forget_conversation(self, session_id: str, *, preserve_ui: bool = False) -> dict[str, str]:
        failures: dict[str, str] = {}
        try:
            if self.durable.has_conversation(session_id):
                self.durable.delete_conversation(session_id)
        except Exception as exc:
            failures["durable"] = str(exc)
        if not preserve_ui:
            try:
                with self.ui_state_store_lock:
                    self.ui_state_store.forget_session(session_id)
            except Exception as exc:
                failures["ui_state"] = str(exc)
        # No phase may short-circuit the rest: executable pending state must
        # be cleared even when an earlier store fails (fail closed).
        try:
            self.conversation_registry.forget(session_id)
        except Exception as exc:
            failures["conversation"] = str(exc)
        try:
            with self.lock:
                self.providers.forget_session(session_id)
        except Exception as exc:
            failures["providers"] = str(exc)
        try:
            self.run_registry.clear_session_outputs(session_id)
        except Exception as exc:
            failures["run_outputs"] = str(exc)
        # Executable pending state must not outlive the chat: expire this
        # session's shell approvals (denied) and drop its restorable research
        # changes. Audit artifacts (run ledgers, managed outputs) are kept.
        with self.lock:
            approval_events = self._expire_shell_results_locked(session_id=session_id)
            orphan_runs = [
                run_id
                for run_id, owner in self._research_change_sessions.items()
                if owner == session_id
            ]
            for run_id in orphan_runs:
                self.research_changes.pop(run_id, None)
                self._research_change_sessions.pop(run_id, None)
        for event in approval_events:
            try:
                self.record_shell_result(event)
            except Exception as exc:
                failures["approvals"] = str(exc)
                break
        self._flush_deferred_durable_events()
        operations: tuple[tuple[str, str, Callable[[Any], Any]], ...] = (
            ("ghost_continuity", "ghost_continuity", lambda s: s.delete_scope("session", session_id=session_id)),
            ("ghost_sleep", "ghost_sleep", lambda s: s.delete_scope("session", session_id=session_id)),
            ("ghost_work_queue", "ghost_work_queue", lambda s: s.delete_scope("session", session_id=session_id)),
            ("ghost_affinity", "ghost_affinity", lambda s: s.delete_scope("session", session_id=session_id)),
            ("run_traces", "run_traces", lambda s: s.delete_session(session_id)),
        )
        for store_name, attr_name, method in operations:
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

    def shutdown_requested(self) -> bool:
        """Read the shutdown signal afresh across worker boundaries."""
        return self._close_requested

    def _begin_shell_execution(self) -> bool:
        """Register a shell executor unless shutdown has already started."""
        with self._shell_execution_condition:
            if self._close_requested:
                return False
            self._shell_executions += 1
            return True

    def _end_shell_execution(self) -> None:
        with self._shell_execution_condition:
            self._shell_executions = max(0, self._shell_executions - 1)
            if self._shell_executions == 0:
                self._shell_execution_condition.notify_all()

    def _wait_shell_executions(self, timeout: float = 2.0) -> bool:
        end = time.monotonic() + timeout
        with self._shell_execution_condition:
            while self._shell_executions:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return False
                self._shell_execution_condition.wait(remaining)
            return True

    @property
    def closed(self) -> bool:
        """Return True when resources have been completely released."""
        return self._resources_closed

    def _wait_background_workers(self) -> bool:
        try:
            return self.shell_approval_workflow.close() and self.ghost_sleep_daemon.wait(timeout=2.0)
        except Exception:
            logger.exception("Background worker cleanup failed")
            return False

    def close(self) -> bool:  # noqa: C901, PLR0912
        """Release durable resources. Returns True only when fully closed.

        A live background Ghost owns its stores: when the short join
        expires, resources are retained and ``closed`` stays False so the
        caller retries or reports incomplete shutdown instead of releasing
        files out from under the daemon (the Windows ``.lock`` handle
        failure). Resource-cleanup failures behave the same: failed pieces
        are retained for a later retry.
        """
        if self._resources_closed:
            return True
        if not self._close_requested:
            self._close_requested = True
            # Same gate as every other production Stop: a bare set() here
            # would let close slip between an Allow's final check and Popen.
            # request_stop emits synchronously (best-effort, registries are
            # still alive) before durable resources release below.
            self.request_stop()

        self.connection_status.close()
        if not self.conversation_runtime.close(self.current_run()):
            return False

        if not self._durable_services.stop_scheduler():
            return False

        if not self._wait_background_workers():
            return False

        if not self._wait_shell_executions():
            return False

        resources_closed = True

        def close_store(store: object) -> bool:
            close = getattr(store, "close", None)
            if not callable(close):
                return True
            for attempt in range(3):
                try:
                    close()
                    return True
                except Exception:
                    if attempt == 2:
                        logger.exception("Resource cleanup failed: %s", type(store).__name__)
                        return False
                    time.sleep(0.05 * (attempt + 1))
            return False

        with self.lock:
            project_writer_leases = tuple(self._project_writer_leases.items())
        for project, lease in project_writer_leases:
            try:
                cast(Any, lease).release()
            except Exception:
                resources_closed = False
            else:
                with self.lock:
                    if self._project_writer_leases.get(project) is lease:
                        self._project_writer_leases.pop(project, None)
        with self.lock:
            self._knowledge_store_enabled = False
            knowledge_store = self._knowledge_store
        if knowledge_store is not None:
            if not close_store(knowledge_store):
                resources_closed = False
            else:
                with self.lock:
                    if self._knowledge_store is knowledge_store:
                        self._knowledge_store = None
        evidence = getattr(self, "evidence_ledgers", None)
        if evidence is not None:
            if not close_store(evidence):
                resources_closed = False
            else:
                self.evidence_ledgers = None
        execution_outputs = getattr(self, "execution_outputs", None)
        if execution_outputs is not None and not close_store(execution_outputs):
            resources_closed = False
        if not close_store(self._durable_services):
            resources_closed = False
        if self._ephemeral_runtime_home is not None:
            try:
                self._ephemeral_runtime_home.cleanup()
            except Exception:
                resources_closed = False
            else:
                self._ephemeral_runtime_home = None
        if resources_closed:
            self._resources_closed = True
            return True
        return False
