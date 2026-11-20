"""Volatile resource bindings; task state and operation receipts remain durable-owned."""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from codey.durable.model import TASK_TERMINAL_STATUSES, CommitResult, DurableMutation
from codey.runtime.core import cancellation
from codey.runtime.core.process_io import ProcessIO
from codey.types.json_value import JsonObject

if TYPE_CHECKING:
    from codey.automation.browser_session import BrowserSession


@dataclass(frozen=True)
class BrowserResource:
    browser: BrowserSession
    binding: JsonObject


class ToolResources:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[tuple[str, str], ProcessIO] = {}
        self._browsers: dict[str, BrowserResource] = {}

    def observe_commit(self, _result: CommitResult, mutation: DurableMutation) -> None:
        task = mutation.task
        if task is not None and task.status in TASK_TERMINAL_STATUSES:
            self.release_browser(task.task_id)

    def close(self) -> None:
        with self._lock:
            task_ids = tuple(self._browsers)
        for task_id in task_ids:
            self.release_browser(task_id)

    def register_process(self, task_id: str, attempt: str, process: ProcessIO) -> Callable[[], None]:
        key = task_id, attempt
        with self._lock:
            if key in self._processes:
                raise ValueError("attempt already owns a live process")
            self._processes[key] = process

        def unregister() -> None:
            with self._lock:
                if self._processes.get(key) is process:
                    del self._processes[key]

        return unregister

    def process(self, task_id: str, attempt: str) -> ProcessIO:
        with self._lock:
            process = self._processes.get((task_id, attempt))
            if process is None:
                raise ValueError("process input is not ready or no longer available")
            return process

    def bind_browser(self, task_id: str, browser: BrowserSession, binding: JsonObject) -> None:
        with self._lock:
            previous = self._browsers.get(task_id)
            if previous is not None:
                if previous.browser is not browser or previous.binding != binding:
                    raise ValueError("tool task browser binding changed")
                return
            browser.provider.retain()
            self._browsers[task_id] = BrowserResource(browser, binding)

    def browser(self, task_id: str, binding: JsonObject) -> BrowserSession:
        with self._lock:
            resource = self._browsers.get(task_id)
            if resource is None or resource.binding != binding:
                raise ValueError("original browser resource is unavailable; operation was not replayed")
            return resource.browser

    def release_browser(self, task_id: str) -> None:
        with self._lock:
            resource = self._browsers.pop(task_id, None)
        if resource is not None:
            with cancellation.cleanup_scope():
                resource.browser.provider.release()
