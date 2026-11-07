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
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from codey.app.shell_service import ShellExecutionTicket
    from codey.repairs.self_repair import SelfRepairSupervisor
from codey.agents.handoff import ConversationContext, ConversationSnapshot
from codey.app import event_bus
from codey.app.active_provider_registry import ActiveProviderRegistry
from codey.app.approval_registry import ApprovalRegistry
from codey.app.conversation_coordinator import ConversationCoordinator, forget_scope, forget_trace
from codey.app.conversation_registry import ConversationRegistry
from codey.app.conversation_runtime import ConversationRuntime
from codey.app.durable_event_bridge import DurableEventBridge
from codey.app.durable_services import DurableAppServices
from codey.app.event_bus import EventBus, EventSubscriber
from codey.app.event_payloads import SCHEMA_VERSION
from codey.app.ghost_daemon import GhostSleepDaemon
from codey.app.ghost_services import GhostStoreAccessors, GhostStores
from codey.app.knowledge_indexer import KnowledgeIndexer
from codey.app.lifecycle import AppLifecycle, ResourceSlot, release_optional_resource
from codey.app.provider_registry import ProviderRegistry
from codey.app.run_registry import RunRegistry, RunSnapshot
from codey.app.run_state_projection import RunStateProjection
from codey.app.runtime_adapters import DurableRuntimeAdapters
from codey.app.shell_approval_coordinator import ShellApprovalCoordinator
from codey.app.task_abort import abort_task_resources
from codey.app.task_lifecycle import TaskLifecycleCoordinator
from codey.app.task_runtime import TaskRuntimeCoordinator
from codey.app.workspace_leases import WorkspaceLeaseCoordinator
from codey.durable.model import (
    CommitResult,
    DurableMutation,
    TaskCheckpoint,
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
from codey.types.json_value import JsonObject
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
        self.fork_request_lock = threading.Lock()
        # Spawn gate: Stop's generation bump and the executor's final
        # check+Popen both hold it (never self.lock: re-entrant deadlock).
        self._shell_spawn_gate = threading.Lock()
        # Shell execution releases the spawn gate while it waits for the
        # process. Keep shutdown from closing the output store during the
        # small post-wait settlement window.
        # Read the module global at call time (not as a default value) so
        # tests can patch SSE_REPLAY_LIMIT around construction.
        self.event_bus = EventBus(
            replay_limit=SSE_REPLAY_LIMIT if replay_limit is None else replay_limit
        )
        self.durable_event_bridge = DurableEventBridge(self.lock, self.emit)
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
        self.task_runtime = TaskRuntimeCoordinator(
            self.run_registry,
            self._active_providers,
            self.lock,
            lambda: self._shell_spawn_gate,
            wake_input=self.input_queue.wake.set,
        )
        self.execution_outputs = ExecutionOutputStore(runtime_state_home)
        from codey.storage.command_permissions import CommandPermissionStore

        self.command_permissions = CommandPermissionStore(runtime_state_home)
        self.shell_approval_records = ShellApprovalRecordStore(runtime_state_home)
        self.evidence_ledgers = EvidenceLedgerStore(state_home) if state_home else None
        self.managed_outputs = ManagedOutputStore(state_home) if state_home else None
        self.shell_approval_coordinator = ShellApprovalCoordinator(self)
        # Rebuild the UI projection for untouched approvals before the first
        # /api/state request after a process restart.  Execution still requires
        # the durable atomic claim, so this only restores display state.
        from codey.app.shell_approval_state import restore_pending_shell_approvals

        restore_pending_shell_approvals(self)
        from codey.app.shell_approval_workflow import ShellApprovalWorkflow

        self.shell_approval_workflow = ShellApprovalWorkflow(self)
        self.durable_scheduler = self._durable_services.scheduler
        self.task_lifecycle = TaskLifecycleCoordinator(
            durable=self.durable,
            run_registry=self.run_registry,
            approval_expiry=lambda **kwargs: self._expire_shell_results_locked(**kwargs),
            shell_approval_workflow_cancel=lambda session_id: self.shell_approval_workflow.cancel_queued(session_id),
            shell_spawn_gate=lambda: self._shell_spawn_gate,
            coordination_lock=self.lock,
            scheduler=self.durable_scheduler,
            emit=lambda event: self.emit(event),
            record_shell_result=lambda event: self.record_shell_result(event),
            flush_deferred_events=lambda: self._flush_deferred_durable_events(),
            pause_input=lambda session_id: self.input_queue.pause(session_id),
            is_closing=lambda: self._close_requested,
            cancel_teach=self.approvals.cancel_teach,
            invalidate_approval_generation=self.approvals.invalidate_generation,
        )
        self._ghost_stores = GhostStores(state_home, self.lock)
        self.ghost_sleep_daemon = GhostSleepDaemon(
            lock=self.lock,
            is_busy=self.is_busy,
            stop_requested=lambda: any(run.stop_event.is_set() for run in self.task_runtime.active_runs()),
            run_once=self._run_ghost_sleep_once,
        )
        self._self_repair: SelfRepairSupervisor | None = None
        self.snapshot_store = (
            SnapshotStore(state_home) if state_home else SnapshotStore()
        )
        self.ui_state_store = (
            UiStateStore(state_home) if state_home else UiStateStore()
        )
        self.workspace_leases = WorkspaceLeaseCoordinator(self.lock, lambda: self.snapshot_store)
        self._project_writer_leases = self.workspace_leases.leases
        self.run_state_projection = RunStateProjection(
            registry=self.run_registry, approvals=self.approvals, workflow=self.shell_approval_workflow,
            durable=self.durable, lock=self.lock, research_runs=lambda: tuple(sorted(self.research_changes)),
            emit=lambda event: self.emit(event),
        )
        def knowledge_for_close() -> KnowledgeStore | None:
            with self.lock:
                self._knowledge_store_enabled = False
                return self._knowledge_store

        def clear_knowledge(store: KnowledgeStore) -> None:
            with self.lock:
                if self._knowledge_store is store:
                    self._knowledge_store = None

        self.lifecycle = AppLifecycle(
            stop=lambda: self.request_stop(),
            close_connections=lambda: self.connection_status.close(),
            close_conversations=lambda: self.conversation_runtime.close(self.current_run()),
            stop_scheduler=lambda: self._durable_services.stop_scheduler(),
            wait_workers=lambda: self._wait_background_workers(),
            close_controls=lambda: self.providers.shutdown_controls(),
            cleanup=(
                lambda: self.workspace_leases.close(),
                ResourceSlot(knowledge_for_close, lambda store: store.close(), clear_knowledge).close,
                ResourceSlot(lambda: self.evidence_ledgers, release_optional_resource,
                             lambda _store: setattr(self, 'evidence_ledgers', None)).close,
                ResourceSlot(lambda: self.execution_outputs, lambda store: store.close()).close,
                ResourceSlot(lambda: self._durable_services, lambda services: services.close()).close,
                ResourceSlot(lambda: self._ephemeral_runtime_home, lambda temporary: temporary.cleanup(),
                             lambda _temporary: setattr(self, '_ephemeral_runtime_home', None), attempts=1).close,
            ),
        )
        self.conversation_coordinator = ConversationCoordinator(
            durable=self.durable, registry=self.conversation_registry, providers=self.providers,
            runs=self.run_registry, ui=self.ui_state_store, lock=self.lock, ui_lock=self.ui_state_store_lock,
            expire_approvals=lambda **kwargs: self._expire_shell_results_locked(**kwargs),
            record_shell_result=lambda event: self.record_shell_result(event),
            flush_events=lambda: self._flush_deferred_durable_events(),
            research_changes=self.research_changes, research_owners=self._research_change_sessions,
            cleanup=(
                ('ghost_continuity', lambda sid: forget_scope(self.ghost_continuity, sid)),
                ('ghost_sleep', lambda sid: forget_scope(self.ghost_sleep, sid)),
                ('ghost_work_queue', lambda sid: forget_scope(self.ghost_work_queue, sid)),
                ('ghost_affinity', lambda sid: forget_scope(self.ghost_affinity, sid)),
                ('run_traces', lambda sid: forget_trace(self.run_traces, sid)),
            ),
        )
        if start_scheduler:
            self.durable_scheduler.start()

    def _dispatch_durable_task(self, checkpoint: object) -> bool:
        from codey.app.task_submit import dispatch_durable_task

        return dispatch_durable_task(checkpoint, state=self)

    def _abort_durable_task(self, checkpoint: TaskCheckpoint, _invocation: object) -> None:
        abort_task_resources(self, checkpoint)

    def register_active_provider(self, run_id: str, provider: object) -> None:
        """Register the raw provider transport for a live durable run."""
        self.task_runtime.register_active_provider(run_id, provider)

    def unregister_active_provider(self, run_id: str, provider: object | None = None) -> None:
        """Drop a run transport, preserving a newer replacement if present."""
        self.task_runtime.unregister_active_provider(run_id, provider)

    def _publish_durable_commit(self, result: CommitResult, mutation: DurableMutation) -> None:
        self.durable_event_bridge.publish_commit(result, mutation)

    def _flush_deferred_durable_events(self) -> None:
        self.durable_event_bridge.flush()

    def _load_durable_conversation_snapshot(
        self, session_id: str
    ) -> ConversationSnapshot | None:
        return self.conversation_coordinator.load_snapshot(session_id)

    def _save_durable_conversation_snapshot(
        self, session_id: str, snapshot: ConversationSnapshot
    ) -> None:
        self.conversation_coordinator.save_snapshot(session_id, snapshot)

    def acquire_project_writer(self, project: str | Path) -> bool:
        return self.workspace_leases.acquire(project)

    def release_project_writer(self, project: str | Path) -> None:
        self.workspace_leases.release(project)

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
        task_id: str = "",
    ) -> ChangeTracker:
        from codey.app.change_tracking import change_tracker_for

        return change_tracker_for(self, project, persistent=persistent, task_id=task_id, limit=MAX_CHANGE_TRACKERS)

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
        return self.task_runtime.reserve_run(
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
        return self.task_runtime.active_run_for(session_id=session_id, project=project)

    def current_run(self) -> RunSnapshot | None:
        return self.task_runtime.current_run()

    def active_runs(self) -> tuple[RunSnapshot, ...]:
        return self.task_runtime.active_runs()

    def stop_flag_for(self, run_id: str) -> threading.Event:
        return self.task_runtime.stop_flag_for(run_id)

    def is_busy(self) -> bool:
        return self.task_runtime.is_busy()

    def run_status(self) -> str:
        return self.task_runtime.run_status()

    def replace_reserved_run(self, current_run_id: str, replacement: RunSnapshot) -> bool:
        return self.task_runtime.replace_reserved_run(current_run_id, replacement)

    def has_active_run_for_project(self, project_key: str) -> bool:
        """True when the active run writes inside the given project."""
        return self.active_run_for(project=project_key) is not None

    def start_run(self, run_id: str) -> bool:
        return self.task_runtime.start_run(run_id)

    def set_run_status(self, status: str) -> None:
        self.task_runtime.set_run_status(status)

    def switch_run_provider(self, run_id: str, provider_id: str) -> bool:
        return self.task_runtime.switch_run_provider(run_id, provider_id)

    def release_run(self, run_id: str) -> None:
        self.task_runtime.release_run(run_id)

    def finish_run(self, run_id: str, event: dict[str, Any]) -> bool:
        return self.task_lifecycle.finish_run(run_id, cast(JsonObject, event))

    def record_shell_result(self, event: dict[str, Any]) -> None:
        self.run_state_projection.record_shell_result(event)

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
        self.task_lifecycle.request_stop(run_id)

    def expire_pending_shell_approvals(self) -> None:
        """Expire every pending shell approval (task stop path).

        A stale Allow card must never execute a command -- and resume work
        -- after the user pressed stop, so stop clears the map and emits a
        denied shell_result for each expired approval under the same lock.
        """

        self.task_lifecycle.expire_pending_shell_approvals()

    def expire_stale_shell_approvals(self, active_run_id: str) -> None:
        """Clear approval cards left behind when the user starts new work."""

        self.task_lifecycle.expire_stale_shell_approvals(active_run_id)

    def _expire_shell_results_locked(self, **kwargs: Any) -> tuple[dict[str, Any], ...]:
        from codey.app.shell_approval_state import expire_shell_results_locked

        return expire_shell_results_locked(self, **kwargs)

    def add_pending_shell_approval(self, approval_id: str, pending: dict[str, Any]) -> None:
        self.shell_approval_coordinator.add_pending(approval_id, pending)

    def record_shell_approval_fact(self, fact: ShellApprovalFact) -> bool:
        return self.shell_approval_coordinator.record_fact(fact)

    def _commit_durable_shell_approval(self, fact: ShellApprovalFact) -> bool:
        return self.shell_approval_coordinator.commit_fact(fact)

    def _claim_pending_shell_approval_locked(
        self, approval_id: str, *, claim_execution: bool = True,
    ) -> dict[str, Any] | None:
        value = self.shell_approval_coordinator.claim_locked(approval_id, claim_execution=claim_execution)
        return cast(dict[str, Any] | None, value)

    def claim_pending_shell_approval(
        self, approval_id: str, *, claim_execution: bool = True,
    ) -> dict[str, Any] | None:
        value = self.shell_approval_coordinator.claim(approval_id, claim_execution=claim_execution)
        return cast(dict[str, Any] | None, value)

    def resolve_shell_approval(self, approval_id: str) -> dict[str, Any] | None:
        value = self.shell_approval_coordinator.resolve(approval_id)
        return cast(dict[str, Any] | None, value)

    def approval_generation(self) -> int:
        return self.shell_approval_coordinator.generation()

    def claim_shell_ticket(
        self, approval_id: str, *, timeout: int, output_limit: int,
    ) -> tuple[dict[str, object] | None, ShellExecutionTicket | None]:
        return self.shell_approval_coordinator.claim_ticket(
            approval_id, timeout=timeout, output_limit=output_limit,
        )

    def pending_shell_approvals(self) -> dict[str, dict[str, Any]]:
        value = self.shell_approval_coordinator.pending()
        return cast(dict[str, dict[str, Any]], value)

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
        return self.run_state_projection.payload()

    def _pending_ui_event(self, active: RunSnapshot | None) -> dict[str, Any] | None:
        return self.run_state_projection.pending_ui_event(active)

    def emit(self, event: dict[str, Any]) -> None:
        from codey.app.task_lifecycle_projection import version_task_event
        event = version_task_event(self.durable, event)
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
        elif payload.get('type') in {'turn', 'reasoning'}:
            from codey.app.tool_history import record_reasoning_display
            record_reasoning_display(self, payload)
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
        return self.conversation_coordinator.conversation_for(session_id)

    def forget_conversation(self, session_id: str, *, preserve_ui: bool = False) -> dict[str, str]:
        return self.conversation_coordinator.forget(session_id, preserve_ui=preserve_ui)

    def provider_session_changed(self, provider_id: str, session_id: str) -> bool:
        with self.lock:
            return self.providers.session_changed(provider_id, session_id)

    def set_provider_session(self, provider_id: str, session_id: str | None) -> None:
        with self.lock:
            self.providers.set_session(provider_id, session_id)

    def shutdown_requested(self) -> bool:
        return self.lifecycle.close_requested

    def _begin_shell_execution(self) -> bool:
        return self.lifecycle.begin_shell_execution()

    def _end_shell_execution(self) -> None:
        self.lifecycle.end_shell_execution()

    def _wait_shell_executions(self, timeout: float = 2.0) -> bool:
        return self.lifecycle.wait_shell_executions(timeout)

    @property
    def closed(self) -> bool:
        return self.lifecycle.resources_closed

    def _wait_background_workers(self) -> bool:
        try:
            forks = getattr(self, 'fork_workspaces', None)
            if forks is not None and not forks.close():
                return False
            return self.shell_approval_workflow.close() and self.ghost_sleep_daemon.wait(timeout=2.0)
        except Exception:
            logger.exception("Background worker cleanup failed")
            return False

    def close(self) -> bool:  # noqa: C901, PLR0912
        return self.lifecycle.close()

    @property
    def _close_requested(self) -> bool:
        return self.lifecycle.close_requested

    @_close_requested.setter
    def _close_requested(self, value: bool) -> None:
        self.lifecycle.close_requested = value

    @property
    def _resources_closed(self) -> bool:
        return self.lifecycle.resources_closed

    @_resources_closed.setter
    def _resources_closed(self, value: bool) -> None:
        self.lifecycle.resources_closed = value

    @property
    def _shell_executions(self) -> int:
        return self.lifecycle.shell_executions
