"""Application assembly: shared context, stores, and UI-agnostic constants.

This module owns AppContext (all local stores, registries, daemons) plus the
small constants both the HTTP server and headless runners need. server.py keeps
only HTTP/SSE routing, the global STATE, and task-runner wiring, so importing
the headless runner never builds global UI state as a side effect.
"""

from __future__ import annotations

import tempfile
import threading
from collections.abc import Callable
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from codey.repairs.self_repair import SelfRepairSupervisor

from codey.agents.handoff import ConversationContext
from codey.app.approval_registry import ApprovalRegistry
from codey.app.conversation_registry import ConversationRegistry
from codey.app import event_bus
from codey.app.event_bus import EventBus, EventSubscriber
from codey.app.ghost_daemon import GhostSleepDaemon
from codey.app.knowledge_indexer import KnowledgeIndexer
from codey.app.provider_registry import ProviderRegistry
from codey.app.run_registry import RunRegistry, RunSnapshot
from codey.ghost.affinity import GhostAffinityStore
from codey.ghost.continuity import GhostContinuityStore
from codey.ghost.hebbian import GhostHebbianStore
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.router import GhostRouteStore
from codey.ghost.sleep import GhostSleepStore
from codey.ghost.store import GhostSignalStore
from codey.ghost.work_queue import GhostWorkQueueStore
from codey.knowledge.store import KnowledgeStore
from codey.research.evidence_ledger import EvidenceLedgerStore
from codey.runs.ledger import RunLedgerStore
from codey.runs.trace import RunTraceStore
from codey.runs.work_checkpoint import WorkCheckpointStore
from codey.runtime.core.operation_state import RuntimeOperationStore
from codey.runtime.effects.effect_records import RuntimeEffectStore
from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
from codey.runtime.log.session_log import RuntimeSessionLog
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.storage.local_store import DEFAULT_STATE_HOME
from codey.storage.managed_outputs import ManagedOutputStore
from codey.storage.ui_state_store import UiStateStore
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
    try:
        close()
    except Exception:
        pass


MAX_CONVERSATION_STATES = 32
MAX_CHANGE_TRACKERS = 32


class AppContext:
    def __init__(
        self,
        state_home: str | Path | None = None,
        *,
        replay_limit: int | None = None,
        sync_ghost_maintenance: bool = False,
    ) -> None:
        self.sync_ghost_maintenance = bool(sync_ghost_maintenance)
        self._ephemeral_runtime_home = tempfile.TemporaryDirectory() if state_home is None else None
        self.state_home = Path(state_home) if state_home else None
        runtime_state_home = (
            self.state_home
            if self.state_home is not None
            else Path(self._ephemeral_runtime_home.name)
        )
        self.lock = threading.Lock()
        # Spawn gate: Stop's generation bump and the executor's final
        # check+Popen both hold it (never self.lock: re-entrant deadlock).
        self._shell_spawn_gate = threading.Lock()
        # Read the module global at call time (not as a default value) so
        # tests can patch SSE_REPLAY_LIMIT around construction.
        self.event_bus = EventBus(
            replay_limit=SSE_REPLAY_LIMIT if replay_limit is None else replay_limit
        )
        self.run_registry = RunRegistry()
        self.approvals = ApprovalRegistry()
        self.research_changes: dict[str, object] = {}
        self._research_change_sessions: dict[str, str] = {}
        self.change_trackers: OrderedDict[str, ChangeTracker] = OrderedDict()
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
        self._self_repair: SelfRepairSupervisor | None = None
        self.snapshot_store = (
            SnapshotStore(state_home) if state_home else SnapshotStore()
        )
        self.ui_state_store = (
            UiStateStore(state_home) if state_home else UiStateStore()
        )
        self._close_requested = False
        self._resources_closed = False

    def _ghost_store(
        self,
        attr_name: str,
        factory: Callable[[Path], object],
    ) -> object | None:
        with self.lock:
            store = getattr(self, attr_name)
            if store is not None:
                return store
            if self.state_home is None:
                return None
            state_home = self.state_home
        # Build outside the lock: store constructors do file IO and must
        # not stall event emit/subscribe on AppContext.lock.
        fresh = factory(state_home)
        with self.lock:
            existing = getattr(self, attr_name)
            if existing is not None:
                winner = existing
            else:
                setattr(self, attr_name, fresh)
                return fresh
        _close_if_discarded(fresh)
        return winner

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
            if self._knowledge_store is not None:
                winner = self._knowledge_store
            else:
                self._knowledge_store = fresh
                return fresh
        _close_if_discarded(fresh)
        return winner

    @knowledge_store.setter
    def knowledge_store(self, value: object | None) -> None:
        with self.lock:
            self._knowledge_store = value
            self._knowledge_store_enabled = value is not None
            if value is not None and self._knowledge_root is None and self.state_home is not None:
                self._knowledge_root = self.state_home / "vault"

    @property
    def self_repair(self) -> SelfRepairSupervisor:
        import functools

        from codey.repairs.self_repair import SelfRepairSupervisor, run_self_repair_job

        with self.lock:
            if self._self_repair is None:
                repair_runner = (
                    functools.partial(
                        run_self_repair_job,
                        state_home=self.state_home,
                        providers=self.providers,
                    )
                    if self.state_home is not None and self.state_home == DEFAULT_STATE_HOME
                    else None
                )
                self._self_repair = SelfRepairSupervisor(self.state_home, runner=repair_runner)
            return self._self_repair

    @self_repair.setter
    def self_repair(self, value: SelfRepairSupervisor | None) -> None:
        with self.lock:
            self._self_repair = value

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
            else:
                self.change_trackers.move_to_end(key)
            self._evict_change_trackers_locked()
            return tracker

    def _evict_change_trackers_locked(self) -> None:
        """Drop oldest in-memory trackers only; durable snapshots stay on disk."""
        while len(self.change_trackers) > MAX_CHANGE_TRACKERS:
            _old_key, old_tracker = self.change_trackers.popitem(last=False)
            try:
                old_tracker.disable_persistence()
            except Exception:
                pass

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
        expired: tuple[dict, ...] = ()
        if str(payload.get("stop_reason") or "") in {"done", "error", "max_turns", "no_progress", "stopped"}:
            with self._shell_spawn_gate, self.lock:
                expired = self.approvals.expire_shell_results(
                    run_id=run_id,
                    output="Run ended; command approval expired.",
                )
        self.emit(payload)
        for expired_event in expired:
            self.record_shell_result(expired_event)
        return True

    def record_shell_result(self, event: dict) -> None:
        payload = self.run_registry.record_shell_result(event)
        self.emit(payload)

    def request_stop(self) -> None:
        """Linearized Stop: flag, teach cancel, and approval expiry share one
        gate with the shell spawn path.

        ``stop_flag.set()`` alone never invalidates an in-flight Allow, so it
        must not happen outside the spawn gate: otherwise Stop can land
        between the executor's final check and ``Popen`` and a side-effect
        command still spawns. Every production Stop path funnels through here
        (UI stop, headless shell-reject); the executor side holds the same
        gate across final-check+Popen in ``shell_service.execute_shell_ticket``.
        """
        with self._shell_spawn_gate:
            self.run_registry.stop_flag.set()
            with self.lock:
                self.approvals.cancel_teach()
                events = self.approvals.expire_shell_results()
        for event in events:
            self.record_shell_result(event)

    def expire_pending_shell_approvals(self) -> None:
        """Expire every pending shell approval (task stop path).

        A stale Allow card must never execute a command -- and resume work
        -- after the user pressed stop, so stop clears the map and emits a
        denied shell_result for each expired approval under the same lock.
        """

        with self._shell_spawn_gate, self.lock:
            events = self.approvals.expire_shell_results()
        for event in events:
            self.record_shell_result(event)

    def expire_stale_shell_approvals(self, active_run_id: str) -> None:
        """Clear approval cards left behind when the user starts new work."""

        with self._shell_spawn_gate, self.lock:
            events = self.approvals.expire_shell_results(
                exclude_run_id=active_run_id,
                output="A new task started; command approval expired.",
            )
        for event in events:
            self.record_shell_result(event)

    def add_pending_shell_approval(self, approval_id: str, pending: dict) -> None:
        with self.lock:
            self.approvals.add_shell(approval_id, pending)

    def pop_pending_shell_approval(self, approval_id: str) -> dict | None:
        with self.lock:
            return self.approvals.pop_shell(approval_id)

    def approval_generation(self) -> int:
        with self.lock:
            return self.approvals.current_generation()

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

    def record_research_changes(
        self, run_id: str, changes: object, session_id: str = ""
    ) -> None:
        with self.lock:
            self.research_changes[run_id] = changes
            owner = str(session_id or "").strip()
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
            payload = event_bus.stamp_run_scope(
                dict(event),
                self.run_registry.current(),
            )
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

    def get_provider(self, provider_id: str):
        """Thin seam for tests: connect via the single provider entry point."""
        from codey.app import provider_services

        return provider_services.open_provider_session(self, provider_id)

    def provider_failover_order(self) -> tuple[str, ...]:
        """Thin seam: open tabs first, then registry order.

        Read via ``getattr`` by ``task_phases.build_hooks`` so task doubles
        can override the order without a provider registry.
        """
        from codey.app import provider_services

        return provider_services.provider_failover_order(self.providers)

    def conversation_for(self, session_id: str) -> ConversationContext:
        return self.conversation_registry.for_session(session_id)

    def forget_conversation(self, session_id: str) -> dict[str, str]:
        failures: dict[str, str] = {}
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
            approval_events = self.approvals.expire_session(session_id)
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

    @property
    def closed(self) -> bool:
        """Return True when resources have been completely released."""
        return self._resources_closed

    def close(self) -> None:
        if self._resources_closed:
            return
        if not self._close_requested:
            self._close_requested = True
            # Same gate as every other production Stop: a bare set() here
            # would let close slip between an Allow's final check and Popen.
            # request_stop emits synchronously (best-effort, registries are
            # still alive) before durable resources release below.
            self.request_stop()

        try:
            self.ghost_sleep_daemon.wait(timeout=2.0)
        except Exception:
            pass

        resources_closed = True
        with self.lock:
            self._knowledge_store_enabled = False
            knowledge_store = self._knowledge_store
        if knowledge_store is not None:
            try:
                getattr(knowledge_store, "close", lambda: None)()
            except Exception:
                resources_closed = False
            else:
                with self.lock:
                    if self._knowledge_store is knowledge_store:
                        self._knowledge_store = None
        evidence = getattr(self, "evidence_ledgers", None)
        if evidence is not None:
            try:
                getattr(evidence, "close", lambda: None)()
            except Exception:
                resources_closed = False
            else:
                self.evidence_ledgers = None
        if self._ephemeral_runtime_home is not None:
            try:
                self._ephemeral_runtime_home.cleanup()
            except Exception:
                resources_closed = False
            else:
                self._ephemeral_runtime_home = None
        if resources_closed:
            self._resources_closed = True

    def __enter__(self) -> AppContext:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()


