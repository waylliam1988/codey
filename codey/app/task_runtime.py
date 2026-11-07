"""Application-owned task runtime coordination.

This object owns the lock ordering around run admission and shell spawning.
The run registry remains the owner of run records; this coordinator owns the
application-facing capability surface and the input wake-up side effect.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from codey.app.active_provider_registry import ActiveProviderRegistry
from codey.app.run_registry import RunRegistry, RunSnapshot


class TaskRuntimeCoordinator:
    """Coordinate run admission, cancellation identities, and gate ordering."""

    def __init__(
        self,
        registry: RunRegistry,
        active_providers: ActiveProviderRegistry,
        coordination_lock: threading.Lock,
        shell_spawn_gate: threading.Lock | Callable[[], threading.Lock],
        *,
        wake_input: Callable[[], None],
    ) -> None:
        self._registry = registry
        self._active_providers = active_providers
        self._coordination_lock = coordination_lock
        self._shell_spawn_gate = shell_spawn_gate if callable(shell_spawn_gate) else lambda: shell_spawn_gate
        self._wake_input = wake_input

    @contextmanager
    def admission_line(self) -> Iterator[None]:
        """Hold the application admission locks in their established order."""
        with self._shell_spawn_gate(), self._coordination_lock:
            yield

    @contextmanager
    def spawn_line(self) -> Iterator[None]:
        """Serialize a final stop check with shell process creation."""
        with self._shell_spawn_gate():
            yield

    def abort_provider(self, run_id: str) -> None:
        """Interrupt only volatile transports after the durable abort barrier."""
        self._registry.request_stop(run_id)
        provider = self._active_providers.get(run_id)
        abandon = getattr(provider, "abandon_inflight", None)
        if callable(abandon):
            try:
                abandon()
            except Exception as exc:
                logging.getLogger(__name__).debug("provider abort cleanup failed for %s: %s", run_id, exc)

    def register_active_provider(self, run_id: str, provider: object) -> None:
        self._active_providers.register(run_id, provider)

    def unregister_active_provider(self, run_id: str, provider: object | None = None) -> None:
        self._active_providers.unregister(run_id, provider)

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
        return self._registry.reserve(
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
        return self._registry.active_for(session_id=session_id, project=project)

    def current_run(self) -> RunSnapshot | None:
        return self._registry.current()

    def active_runs(self) -> tuple[RunSnapshot, ...]:
        return self._registry.active_runs()

    def stop_flag_for(self, run_id: str) -> threading.Event:
        return self._registry.stop_flag_for(run_id)

    @property
    def stop_flag(self) -> threading.Event:
        return self._registry.stop_flag

    def session_for(self, run_id: str) -> str:
        return self._registry.session_for(run_id)

    def request_stop(self, run_id: str | None = None) -> bool:
        return self._registry.request_stop(run_id)

    def clear_stop(self, run_id: str | None = None) -> None:
        self._registry.clear_stop(run_id)

    def bind_api_selection(self, run_id: str, selection: object) -> None:
        self._registry.bind_api_selection(run_id, selection)

    def is_busy(self) -> bool:
        return self._registry.is_busy()

    def run_status(self) -> str:
        return self._registry.status()

    def replace_reserved_run(self, current_run_id: str, replacement: RunSnapshot) -> bool:
        return self._registry.replace_active(current_run_id, replacement)

    def start_run(self, run_id: str) -> bool:
        return self._registry.start(run_id)

    def set_run_status(self, status: str) -> None:
        self._registry.set_status(status)

    def switch_run_provider(self, run_id: str, provider_id: str) -> bool:
        return self._registry.switch_provider(run_id, provider_id)

    def release_run(self, run_id: str) -> None:
        self._registry.release(run_id)
        self._wake_input()

    def wait_for_slot(self, timeout: float | None = None) -> bool:
        return self._registry.wait_for_slot(timeout)


__all__ = ["TaskRuntimeCoordinator"]
