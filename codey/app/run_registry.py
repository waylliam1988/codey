"""Session-scoped run lifecycle registry.

Runs are independent admission units. A session remains serial because its
conversation/provider window is ordered, while different sessions can own a
run at the same time. ``current()`` is only a presentation convenience; it
is never used as the admission lock.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from codey.providers.catalog import DEFAULT_PROVIDER_ID
from codey.providers.diagnostics import ProviderFailure


@dataclass(frozen=True)
class RunSnapshot:
    run_id: str
    session_id: str
    project: str | None
    task: str
    provider_id: str
    status: str = "queued"
    stop_event: threading.Event = field(default_factory=threading.Event, compare=False, repr=False)


class RunRegistry:
    """Own independent run records and their cancellation identities."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.stop_flag = threading.Event()  # admission-level stop before a run exists
        self._slot_available = threading.Event()
        self._slot_available.set()
        self._manual_busy = False
        self._active_runs: dict[str, RunSnapshot] = {}
        # Primary presentation snapshot; admission and cancellation use the
        # keyed collection above so different sessions can run concurrently.
        self._active_run: RunSnapshot | None = None
        self._current_run_id = ""
        self._api_selections: dict[str, Any] = {}
        self._last_summary: str | None = None
        self._last_stop_reason: str | None = None
        self._last_provider_failure: ProviderFailure | None = None
        self._last_terminal_event: dict[str, Any] | None = None
        self._last_shell_result: dict[str, Any] | None = None
        self._status = "idle"
        self._project: str | None = None
        self._task: str | None = None
        self._provider_id = DEFAULT_PROVIDER_ID

    def _sync_slot_event(self) -> None:
        if not self._active_runs and not self._manual_busy:
            self._slot_available.set()
        else:
            self._slot_available.clear()

    def _sync_primary_locked(self) -> None:
        self._active_run = self._primary_locked()

    def _primary_locked(self) -> RunSnapshot | None:
        if self._current_run_id:
            return self._active_runs.get(self._current_run_id)
        return next(iter(self._active_runs.values()), None)

    def is_busy(self) -> bool:
        with self._lock:
            return bool(self._active_runs) or self._manual_busy

    def set_busy(self, value: bool) -> None:
        with self._lock:
            self._manual_busy = bool(value)
            self._sync_slot_event()

    def current(self) -> RunSnapshot | None:
        with self._lock:
            return self._primary_locked()

    def active_runs(self) -> tuple[RunSnapshot, ...]:
        with self._lock:
            return tuple(self._active_runs.values())

    def stop_flag_for(self, run_id: str) -> threading.Event:
        with self._lock:
            run = self._active_runs.get(str(run_id))
            return run.stop_event if run is not None else self.stop_flag

    def session_for(self, run_id: str) -> str:
        with self._lock:
            run = self._active_runs.get(str(run_id))
            return run.session_id if run is not None else ""

    def request_stop(self, run_id: str | None = None) -> bool:
        with self._lock:
            target = str(run_id or self._current_run_id)
            run = self._active_runs.get(target) if target else None
            if run is None:
                if not run_id:
                    self.stop_flag.set()
                return False
            run.stop_event.set()
            return True

    def clear_stop(self, run_id: str | None = None) -> None:
        with self._lock:
            target = str(run_id or self._current_run_id)
            run = self._active_runs.get(target) if target else None
            if run is None:
                self.stop_flag.clear()
            else:
                run.stop_event.clear()

    def bind_api_selection(self, run_id: str, config: Any) -> None:
        with self._lock:
            if run_id in self._active_runs:
                self._api_selections[run_id] = config

    def api_selection_for(self, run_id: str) -> Any:
        with self._lock:
            return self._api_selections.get(run_id)

    def replace_active(self, current_run_id: str, run: RunSnapshot) -> bool:
        with self._lock:
            if current_run_id not in self._active_runs:
                return False
            previous = self._active_runs[current_run_id]
            replacement = replace(run, stop_event=previous.stop_event)
            self._active_runs.pop(current_run_id, None)
            self._active_runs[replacement.run_id] = replacement
            if self._current_run_id == current_run_id:
                self._current_run_id = replacement.run_id
            self._remember_primary_locked(replacement)
            self._sync_primary_locked()
            self._sync_slot_event()
            return True

    def _remember_primary_locked(self, run: RunSnapshot) -> None:
        self._project = run.project
        self._task = run.task
        self._provider_id = run.provider_id
        self._status = run.status

    def project(self) -> str | None:
        with self._lock:
            run = self._primary_locked()
            return run.project if run else self._project

    def task(self) -> str | None:
        with self._lock:
            run = self._primary_locked()
            return run.task if run else self._task

    def provider_id(self) -> str:
        with self._lock:
            run = self._primary_locked()
            return run.provider_id if run else self._provider_id

    def status(self) -> str:
        with self._lock:
            run = self._primary_locked()
            return run.status if run else self._status

    def set_last_summary(self, value: str | None) -> None:
        with self._lock:
            self._last_summary = value

    def last_summary(self) -> str | None:
        with self._lock:
            return self._last_summary

    def set_last_stop_reason(self, value: str | None) -> None:
        with self._lock:
            self._last_stop_reason = value

    def last_stop_reason(self) -> str | None:
        with self._lock:
            return self._last_stop_reason

    def set_last_provider_failure(self, value: ProviderFailure | None) -> None:
        with self._lock:
            self._last_provider_failure = value

    def last_provider_failure(self) -> ProviderFailure | None:
        with self._lock:
            return self._last_provider_failure

    def set_last_terminal_event(self, value: dict[str, Any] | None) -> None:
        with self._lock:
            self._last_terminal_event = dict(value) if value is not None else None

    def last_terminal_event(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._last_terminal_event) if self._last_terminal_event else None

    def set_last_shell_result(self, value: dict[str, Any] | None) -> None:
        with self._lock:
            self._last_shell_result = dict(value) if value is not None else None

    def last_shell_result(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._last_shell_result) if self._last_shell_result else None

    def reserve(
        self,
        *,
        session_id: str,
        project: str | None,
        task: str,
        provider_id: str,
        run_id: str = "",
        abort_if_stopped: bool = False,
    ) -> RunSnapshot | None:
        with self._lock:
            if abort_if_stopped and self.stop_flag.is_set():
                return None
            identity = run_id or "run_" + uuid.uuid4().hex
            if identity in self._active_runs:
                return self._active_runs[identity]
            if any(run.session_id == session_id for run in self._active_runs.values()):
                return None
            self.stop_flag.clear()
            run = RunSnapshot(identity, session_id, project, task, provider_id)
            self._active_runs[identity] = run
            self._current_run_id = identity
            self._remember_primary_locked(run)
            self._sync_primary_locked()
            self._sync_slot_event()
            return run

    def active_for(self, *, session_id: str = "", project: str | None = None) -> RunSnapshot | None:
        with self._lock:
            candidates = tuple(self._active_runs.values())
        for run in reversed(candidates):
            if session_id and run.session_id != session_id:
                continue
            if project and not same_project(run.project or "", project):
                continue
            return run
        return None

    def start(self, run_id: str) -> bool:
        with self._lock:
            run = self._active_runs.get(run_id)
            if run is None:
                return False
            self._active_runs[run_id] = replace(run, status="running")
            if self._current_run_id == run_id:
                self._status = "running"
            return True

    def set_status(self, status: str, run_id: str = "") -> None:
        with self._lock:
            target = run_id or self._current_run_id
            run = self._active_runs.get(target)
            if run is not None:
                self._active_runs[target] = replace(run, status=status)
                if target == self._current_run_id:
                    self._status = status
            else:
                self._status = status

    def switch_provider(self, run_id: str, provider_id: str) -> bool:
        with self._lock:
            run = self._active_runs.get(run_id)
            if run is None:
                return False
            self._active_runs[run_id] = replace(run, provider_id=provider_id)
            if self._current_run_id == run_id:
                self._provider_id = provider_id
            return True

    def release(self, run_id: str) -> None:
        with self._lock:
            self._active_runs.pop(run_id, None)
            self._api_selections.pop(run_id, None)
            if self._current_run_id == run_id:
                self._current_run_id = next(reversed(self._active_runs), "") if self._active_runs else ""
            self._sync_primary_locked()
            self._sync_slot_event()
            if not self._active_runs:
                self._status = "idle"

    def finish(self, run_id: str, event: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            run = self._active_runs.pop(run_id, None)
            if run is None:
                return None
            payload = dict(event)
            payload["run_id"] = run_id
            payload.setdefault("session_id", run.session_id)
            self._api_selections.pop(run_id, None)
            if self._current_run_id == run_id:
                self._current_run_id = next(reversed(self._active_runs), "") if self._active_runs else ""
            self._sync_primary_locked()
            self._last_terminal_event = payload
            self._last_summary = str(payload.get("summary") or "")
            self._last_stop_reason = str(payload.get("stop_reason") or "done")
            self._status = "error" if self._last_stop_reason == "error" else self._last_stop_reason
            self._sync_slot_event()
            return payload

    def record_shell_result(self, event: dict[str, Any]) -> dict[str, Any]:
        payload = dict(event)
        with self._lock:
            self._last_shell_result = payload
        return payload

    def clear_session_outputs(self, session_id: str) -> None:
        with self._lock:
            if self._last_terminal_event and self._last_terminal_event.get("session_id") == session_id:
                self._last_terminal_event = None
                self._last_summary = ""
                self._last_stop_reason = ""
            if self._last_shell_result and self._last_shell_result.get("session_id") == session_id:
                self._last_shell_result = None

    def payload(
        self,
        *,
        pending_event: Callable[[RunSnapshot | None], dict[str, Any] | None],
        research_restore_runs: tuple[str, ...],
    ) -> dict[str, Any]:
        with self._lock:
            active = self._primary_locked()
            active_runs = [
                {
                    "run_id": run.run_id,
                    "session_id": run.session_id,
                    "run_status": run.status,
                    "project": run.project,
                    "provider": run.provider_id,
                }
                for run in self._active_runs.values()
            ]
            terminal = dict(self._last_terminal_event) if self._last_terminal_event else None
            shell_result = dict(self._last_shell_result) if self._last_shell_result else None
            provider_failure = self._last_provider_failure.to_dict() if self._last_provider_failure else None
            payload = {
                "run_id": active.run_id if active else str((terminal or {}).get("run_id") or ""),
                "session_id": active.session_id if active else str((terminal or {}).get("session_id") or ""),
                "busy": bool(active_runs) or self._manual_busy,
                "run_status": active.status if active else self._status,
                "project": active.project if active else self._project,
                "task": active.task if active else self._task,
                "provider": active.provider_id if active else self._provider_id,
                "active_runs": active_runs,
                "summary": self._last_summary,
                "stop_reason": self._last_stop_reason,
                "provider_failure": provider_failure,
                "last_terminal_event": terminal,
                "last_shell_result": shell_result,
                "research_restore_runs": list(research_restore_runs),
            }
        payload["pending_event"] = pending_event(active)
        return payload

    def wait_for_slot(self, timeout: float | None = None) -> bool:
        return self._slot_available.wait(timeout)


def same_project(left: str, right: str) -> bool:
    if not left or not right:
        return False
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return left == right
