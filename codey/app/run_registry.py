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
        self.active_run: RunSnapshot | None = None
        self.project: str | None = None
        self.task: str | None = None
        self.provider_id = DEFAULT_PROVIDER_ID
        self.status = "idle"
        self.last_summary: str | None = None
        self.last_stop_reason: str | None = None
        self.last_provider_failure: ProviderFailure | None = None
        self.last_terminal_event: dict | None = None
        self.last_shell_result: dict | None = None

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    @busy.setter
    def busy(self, value: bool) -> None:
        with self._lock:
            self._busy = bool(value)

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
            if self.active_run is not None or self._busy:
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
            self.active_run = run
            self._busy = True
            self.project = project
            self.task = task
            self.provider_id = provider_id
            self.status = run.status
            return run

    def active_for(
        self,
        *,
        session_id: str = "",
        project: str | None = None,
    ) -> RunSnapshot | None:
        with self._lock:
            run = self.active_run
        if run is None:
            return None
        if session_id and run.session_id != session_id:
            return None
        if project and not same_project(run.project or "", project):
            return None
        return run

    def start(self, run_id: str) -> bool:
        with self._lock:
            if self.active_run is None or self.active_run.run_id != run_id:
                return False
            self.active_run = replace(self.active_run, status="running")
            self.status = "running"
            return True

    def set_status(self, status: str) -> None:
        with self._lock:
            if self.active_run is not None:
                self.active_run = replace(self.active_run, status=status)
            self.status = status

    def switch_provider(self, run_id: str, provider_id: str) -> bool:
        with self._lock:
            if self.active_run is None or self.active_run.run_id != run_id:
                return False
            self.active_run = replace(self.active_run, provider_id=provider_id)
            return True

    def release(self, run_id: str) -> None:
        with self._lock:
            if self.active_run is None or self.active_run.run_id != run_id:
                return
            self.active_run = None
            self._busy = False
            self.status = "idle"

    def finish(self, run_id: str, event: dict) -> dict | None:
        payload = dict(event)
        payload["run_id"] = run_id
        with self._lock:
            run = self.active_run
            if run is None or run.run_id != run_id:
                return None
            payload.setdefault("session_id", run.session_id)
            self.active_run = None
            self._busy = False
            self.last_terminal_event = payload
            self.last_summary = str(payload.get("summary") or "")
            self.last_stop_reason = str(payload.get("stop_reason") or "done")
            self.status = "error" if self.last_stop_reason == "error" else "done"
            return payload

    def record_shell_result(self, event: dict) -> dict:
        payload = dict(event)
        with self._lock:
            self.last_shell_result = payload
        return payload

    def clear_session_outputs(self, session_id: str) -> None:
        with self._lock:
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

    def payload(
        self,
        *,
        pending_event: Callable[[RunSnapshot | None], dict | None],
        research_restore_runs: tuple[str, ...],
    ) -> dict:
        with self._lock:
            active = self.active_run
            terminal = dict(self.last_terminal_event) if self.last_terminal_event else None
            shell_result = dict(self.last_shell_result) if self.last_shell_result else None
            run_id = active.run_id if active else str((terminal or {}).get("run_id") or "")
            session_id = active.session_id if active else str((terminal or {}).get("session_id") or "")
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
