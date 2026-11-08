"""Own conversation snapshots and deletion across application projections."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol

from codey.agents.handoff import ConversationContext, ConversationSnapshot
from codey.app.conversation_registry import ConversationRegistry
from codey.app.provider_registry import ProviderRegistry
from codey.app.run_registry import RunRegistry
from codey.app.task_lifecycle import ShellApprovalExpiry
from codey.durable.errors import CommitConflict
from codey.durable.model import thaw_json
from codey.durable.session import DurableSession
from codey.runs.trace import RunTraceStore
from codey.storage.ui_state_store import UiStateStore
from codey.types.json_value import JsonObject


class SessionScopeStore(Protocol):
    def delete_scope(self, scope: str, *, session_id: str = "") -> object: ...


def forget_scope(store: SessionScopeStore | None, session_id: str) -> None:
    if store is not None:
        store.delete_scope("session", session_id=session_id)


def forget_trace(store: RunTraceStore | None, session_id: str) -> None:
    if store is not None:
        store.delete_session(session_id)


class ConversationCoordinator:
    def __init__(
        self, *, durable: DurableSession, registry: ConversationRegistry,
        providers: ProviderRegistry, runs: RunRegistry, ui: UiStateStore,
        lock: threading.Lock, ui_lock: threading.Lock,
        expire_approvals: ShellApprovalExpiry, record_shell_result: Callable[[JsonObject], None],
        flush_events: Callable[[], None], research_changes: dict[str, object],
        research_owners: dict[str, str], cleanup: tuple[tuple[str, Callable[[str], None]], ...],
    ) -> None:
        self._durable = durable
        self._registry = registry
        self._providers = providers
        self._runs = runs
        self._ui = ui
        self._lock = lock
        self._ui_lock = ui_lock
        self._expire_approvals = expire_approvals
        self._record_shell_result = record_shell_result
        self._flush_events = flush_events
        self._research_changes = research_changes
        self._research_owners = research_owners
        self._cleanup = cleanup

    def conversation_for(self, session_id: str) -> ConversationContext:
        return self._registry.for_session(session_id)

    def load_snapshot(self, session_id: str) -> ConversationSnapshot | None:
        if not self._durable.has_conversation(session_id):
            return None
        entry = self._durable.latest_context_snapshot(session_id)
        return None if entry is None else ConversationSnapshot.from_payload(thaw_json(entry.payload))

    def save_snapshot(self, session_id: str, snapshot: ConversationSnapshot) -> None:
        if not self._durable.has_conversation(session_id):
            self._durable.create_conversation(session_id)
        self._durable.save_context_snapshot(session_id, snapshot.to_payload())

    def forget(self, session_id: str, *, preserve_ui: bool = False) -> dict[str, str]:
        failures: dict[str, str] = {}
        try:
            if self._durable.has_conversation(session_id):
                self._durable.delete_conversation(session_id)
        except CommitConflict:
            raise
        except Exception as exc:
            failures["durable"] = str(exc)
            return failures
        # Cleanup must continue after failures: executable state cannot outlive the chat.
        try:
            self._registry.forget(session_id)
        except Exception as exc:
            failures["conversation"] = str(exc)
        try:
            with self._lock:
                self._providers.forget_session(session_id)
        except Exception as exc:
            failures["providers"] = str(exc)
        try:
            self._runs.clear_session_outputs(session_id)
        except Exception as exc:
            failures["run_outputs"] = str(exc)
        with self._lock:
            try:
                events = self._expire_approvals(session_id=session_id)
            except Exception as exc:
                failures["approvals"] = str(exc)
                events = ()
            orphan_runs = [run_id for run_id, owner in self._research_owners.items() if owner == session_id]
            for run_id in orphan_runs:
                self._research_changes.pop(run_id, None)
                self._research_owners.pop(run_id, None)
        for event in events:
            try:
                self._record_shell_result(event)
            except Exception as exc:
                failures["approvals"] = str(exc)
                break
        try:
            self._flush_events()
        except Exception as exc:
            failures["events"] = str(exc)
        for name, cleanup in self._cleanup:
            try:
                cleanup(session_id)
            except Exception as exc:
                failures[name] = str(exc)
        # Keep the chat discoverable for an idempotent retry of partial cleanup.
        if not preserve_ui and not failures:
            try:
                with self._ui_lock:
                    self._ui.forget_session(session_id)
            except Exception as exc:
                failures["ui_state"] = str(exc)
        return failures
