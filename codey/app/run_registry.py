"""Single active-run registry for the local app."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from codey.providers import DEFAULT_PROVIDER_ID
from codey.providers.diagnostics import ProviderFailure


@dataclass(frozen=True)
class RunSnapshot:
    run_id: str
    session_id: str
    project: str | None
    task: str
    provider_id: str
    status: str = "queued"


class RunRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.stop_flag = threading.Event()
        self._busy = False
        self._active_run: RunSnapshot | None = None
        self._project: str | None = None
        self._task: str | None = None
        self._provider_id = DEFAULT_PROVIDER_ID
        self._status = "idle"
        self._last_summary: str | None = None
        self._last_stop_reason: str | None = None
        self._last_provider_failure: ProviderFailure | None = None
        self._last_terminal_event: dict | None = None
        self._last_shell_result: dict | None = None

    def is_busy(self) -> bool:
        with self._lock:
            return self._busy

    def set_busy(self, value: bool) -> None:
        with self._lock:
            self._busy = bool(value)

    def current(self) -> RunSnapshot | None:
        with self._lock:
            return self._active_run

    def replace_active(self, current_run_id: str, run: RunSnapshot) -> bool:
        with self._lock:
            if self._active_run is None or self._active_run.run_id != current_run_id:
                return False
            self._active_run = run
            self._project = run.project
            self._task = run.task
            self._provider_id = run.provider_id
            self._status = run.status
            self._busy = True
            return True

    def set_active(self, run: RunSnapshot | None) -> None:
        with self._lock:
            self._active_run = run
            self._busy = run is not None
            if run is not None:
                self._project = run.project
                self._task = run.task
                self._provider_id = run.provider_id
                self._status = run.status

    def project(self) -> str | None:
        with self._lock:
            return self._project

    def set_project(self, value: str | None) -> None:
        with self._lock:
            self._project = value

    def task(self) -> str | None:
        with self._lock:
            return self._task

    def set_task(self, value: str | None) -> None:
        with self._lock:
            self._task = value

    def provider_id(self) -> str:
        with self._lock:
            return self._provider_id

    def set_provider_id(self, value: str) -> None:
        with self._lock:
            self._provider_id = value

    def status(self) -> str:
        with self._lock:
            return self._status

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

    def set_last_terminal_event(self, value: dict | None) -> None:
        payload = dict(value) if value is not None else None
        with self._lock:
            self._last_terminal_event = payload

    def last_terminal_event(self) -> dict | None:
        with self._lock:
            return dict(self._last_terminal_event) if self._last_terminal_event else None

    def set_last_shell_result(self, value: dict | None) -> None:
        payload = dict(value) if value is not None else None
        with self._lock:
            self._last_shell_result = payload

    def last_shell_result(self) -> dict | None:
        with self._lock:
            return dict(self._last_shell_result) if self._last_shell_result else None

    def reserve(
        self,
        *,
        session_id: str,
        project: str | None,
        task: str,
        provider_id: str,
        abort_if_stopped: bool = False,
    ) -> RunSnapshot | None:
        with self._lock:
            if self._active_run is not None or self._busy:
                return None
            if abort_if_stopped and self.stop_flag.is_set():
                return None
            self.stop_flag.clear()
            run = RunSnapshot(
                run_id="run_" + uuid.uuid4().hex,
                session_id=session_id,
                project=project,
                task=task,
                provider_id=provider_id,
            )
            self._active_run = run
            self._busy = True
            self._project = project
            self._task = task
            self._provider_id = provider_id
            self._status = run.status
            return run

    def active_for(
        self,
        *,
        session_id: str = "",
        project: str | None = None,
    ) -> RunSnapshot | None:
        with self._lock:
            run = self._active_run
        if run is None:
            return None
        if session_id and run.session_id != session_id:
            return None
        if project and not same_project(run.project or "", project):
            return None
        return run

    def start(self, run_id: str) -> bool:
        with self._lock:
            if self._active_run is None or self._active_run.run_id != run_id:
                return False
            self._active_run = replace(self._active_run, status="running")
            self._status = "running"
            return True

    def set_status(self, status: str) -> None:
        with self._lock:
            if self._active_run is not None:
                self._active_run = replace(self._active_run, status=status)
            self._status = status

    def switch_provider(self, run_id: str, provider_id: str) -> bool:
        with self._lock:
            if self._active_run is None or self._active_run.run_id != run_id:
                return False
            self._active_run = replace(self._active_run, provider_id=provider_id)
            self._provider_id = provider_id
            return True

    def release(self, run_id: str) -> None:
        with self._lock:
            if self._active_run is None or self._active_run.run_id != run_id:
                return
            self._active_run = None
            self._busy = False
            self._status = "idle"

    def finish(self, run_id: str, event: dict) -> dict | None:
        payload = dict(event)
        payload["run_id"] = run_id
        with self._lock:
            run = self._active_run
            if run is None or run.run_id != run_id:
                return None
            payload.setdefault("session_id", run.session_id)
            self._active_run = None
            self._busy = False
            self._last_terminal_event = payload
            self._last_summary = str(payload.get("summary") or "")
            self._last_stop_reason = str(payload.get("stop_reason") or "done")
            self._status = "error" if self._last_stop_reason == "error" else "done"
            return payload

    def record_shell_result(self, event: dict) -> dict:
        payload = dict(event)
        with self._lock:
            self._last_shell_result = payload
        return payload

    def clear_session_outputs(self, session_id: str) -> None:
        with self._lock:
            if (
                self._last_terminal_event is not None
                and self._last_terminal_event.get("session_id") == session_id
            ):
                self._last_terminal_event = None
                self._last_summary = ""
                self._last_stop_reason = ""
            if (
                self._last_shell_result is not None
                and self._last_shell_result.get("session_id") == session_id
            ):
                self._last_shell_result = None

    def payload(
        self,
        *,
        pending_event: Callable[[RunSnapshot | None], dict | None],
        research_restore_runs: tuple[str, ...],
    ) -> dict:
        with self._lock:
            active = self._active_run
            terminal = dict(self._last_terminal_event) if self._last_terminal_event else None
            shell_result = dict(self._last_shell_result) if self._last_shell_result else None
            run_id = active.run_id if active else str((terminal or {}).get("run_id") or "")
            session_id = active.session_id if active else str((terminal or {}).get("session_id") or "")
            provider_failure = self._last_provider_failure.to_dict() if self._last_provider_failure else None
            return {
                "run_id": run_id,
                "session_id": session_id,
                "busy": active is not None,
                "run_status": active.status if active else self._status,
                "project": active.project if active else self._project,
                "task": active.task if active else self._task,
                "provider": active.provider_id if active else self._provider_id,
                "summary": self._last_summary,
                "stop_reason": self._last_stop_reason,
                "provider_failure": provider_failure,
                "pending_event": pending_event(active),
                "last_terminal_event": terminal,
                "last_shell_result": shell_result,
                "research_restore_runs": list(research_restore_runs),
            }


def same_project(left: str, right: str) -> bool:
    if not left or not right:
        return False
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return left == right
