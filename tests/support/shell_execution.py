"""Shell executor fixture with the real public synchronization owners."""

from __future__ import annotations

import threading

from codey.app.active_provider_registry import ActiveProviderRegistry
from codey.app.lifecycle import AppLifecycle
from codey.app.run_registry import RunRegistry
from codey.app.task_runtime import TaskRuntimeCoordinator


class ShellExecutionHarness:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._shell_spawn_gate = threading.Lock()
        self.run_registry = RunRegistry()
        self.task_runtime = TaskRuntimeCoordinator(
            self.run_registry, ActiveProviderRegistry(), self.lock, self._shell_spawn_gate,
            wake_input=lambda: None,
        )

        def stop() -> None:
            self.task_runtime.request_stop()

        self.lifecycle = AppLifecycle(
            stop=stop, close_connections=lambda: None,
            close_conversations=lambda: True, stop_scheduler=lambda: True,
            wait_workers=lambda: True, close_controls=lambda: True, cleanup=(),
        )
        self.events: list[dict[str, object]] = []

    def approval_generation(self) -> int:
        return 0

    def emit(self, event: dict[str, object]) -> None:
        self.events.append(event)
