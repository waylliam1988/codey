"""Background Ghost sleep worker coordination."""

from __future__ import annotations

import threading
from typing import Callable, Mapping


class GhostSleepDaemon:
    def __init__(
        self,
        *,
        lock: threading.Lock,
        is_busy: Callable[[], bool],
        stop_requested: Callable[[], bool],
        run_once: Callable[[dict[str, object]], None],
    ) -> None:
        self.lock = lock
        self.is_busy = is_busy
        self.stop_requested = stop_requested
        self.run_once = run_once
        self.running = False
        self.pending = False
        self.pending_payload: dict[str, object] | None = None
        self.thread: threading.Thread | None = None

    def kick(self, payload: Mapping[str, object]) -> bool:
        current_payload = dict(payload)
        with self.lock:
            if self.is_busy():
                self.pending = True
                self.pending_payload = current_payload
                return False
            if self.running:
                self.pending = True
                self.pending_payload = current_payload
                return False
            self.running = True
            self.pending = False
            self.pending_payload = None

        thread = threading.Thread(
            target=self._worker,
            args=(current_payload,),
            name="codey-ghost-sleep",
            daemon=True,
        )
        with self.lock:
            self.thread = thread
        thread.start()
        return True

    def wait(self, timeout: float | None = None) -> bool:
        with self.lock:
            thread = self.thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _worker(self, payload: dict[str, object]) -> None:
        current_payload = payload
        while True:
            try:
                self.run_once(current_payload)
            except Exception:
                pass
            with self.lock:
                if self.pending and not self.is_busy():
                    current_payload = self.pending_payload or current_payload
                    self.pending = False
                    self.pending_payload = None
                    continue
                self.running = False
                self.pending_payload = None
                if self.thread is threading.current_thread():
                    self.thread = None
                return

    def should_cancel_current(self) -> bool:
        return self.is_busy() or self.stop_requested()


__all__ = ["GhostSleepDaemon"]

