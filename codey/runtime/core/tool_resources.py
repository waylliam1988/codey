"""Volatile resource bindings; task state and operation receipts remain durable-owned."""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

from codey.durable.model import TASK_TERMINAL_STATUSES, CommitResult, DurableMutation
from codey.runtime.core import cancellation
from codey.runtime.core.process_io import ProcessIO
from codey.types.json_value import JsonObject

if TYPE_CHECKING:
    from codey.automation.browser_session import BrowserSession
    from codey.durable.session import DurableSession


@dataclass(frozen=True)
class BrowserResource:
    browser: BrowserSession
    binding: JsonObject


class ToolResources:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[tuple[str, str], ProcessIO] = {}
        self._browsers: dict[str, BrowserResource] = {}
        self._task_browsers: dict[str, BrowserSession] = {}

    def observe_commit(self, _result: CommitResult, mutation: DurableMutation) -> None:
        tasks = (*((mutation.task,) if mutation.task is not None else ()), *mutation.task_updates)
        for task in tasks:
            if task.status in TASK_TERMINAL_STATUSES:
                self.release_browser(task.task_id)
                self.release_task_browser(task.task_id)

    def close(self) -> None:
        with self._lock:
            task_ids = tuple(self._browsers)
            owner_ids = tuple(self._task_browsers)
        for task_id in task_ids:
            self.release_browser(task_id)
        for owner_id in owner_ids:
            self.release_task_browser(owner_id)

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
            browser.retain()
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
                resource.browser.release()

    def acquire_task_browser(self, owner_task_id: str, factory: Callable[[], BrowserSession]) -> BrowserSession:
        if not owner_task_id:
            raise ValueError("task browser requires an owner")
        with self._lock:
            existing = self._task_browsers.get(owner_task_id)
            if existing is not None:
                return existing
            browser = factory()
            browser.retain()
            self._task_browsers[owner_task_id] = browser
            return browser

    def task_browser(self, owner_task_id: str) -> BrowserSession:
        with self._lock:
            browser = self._task_browsers.get(owner_task_id)
            if browser is None:
                raise ValueError("original task browser is unavailable")
            return browser

    def release_task_browser(self, owner_task_id: str) -> None:
        with self._lock:
            browser = self._task_browsers.pop(owner_task_id, None)
        if browser is not None:
            with cancellation.cleanup_scope():
                try:
                    browser.close()
                finally:
                    browser.release()



_resources_lock = threading.Lock()
_resources: WeakKeyDictionary[DurableSession, ToolResources] = WeakKeyDictionary()


def tool_resources_for(session: DurableSession) -> ToolResources:
    """Bind runtime resources once to the durable session's existing lifecycle."""
    with session.control_line(), _resources_lock:
        resources = _resources.get(session)
        if resources is None:
            resources = ToolResources()
            session.subscribe_close(resources.close)
            session.subscribe_commits(resources.observe_commit)
            _resources[session] = resources
        return resources
