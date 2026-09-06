"""Single-flight knowledge index rebuild coordination."""

from __future__ import annotations

import threading
from typing import Callable

from codey.utils.refs import clip, digest_text


class KnowledgeIndexer:
    def __init__(
        self,
        *,
        lock: threading.Lock,
        store: Callable[[], object | None],
    ) -> None:
        self.lock = lock
        self.store = store
        self.running = False
        self.pending = False
        self.error_count = 0
        self.last_error = ""
        self.last_error_ref = ""

    def schedule(self) -> None:
        if self.store() is None:
            return
        with self.lock:
            if self.running:
                self.pending = True
                return
            self.running = True
            self.pending = False
        threading.Thread(
            target=self._worker,
            name="codey-knowledge-rebuild",
            daemon=True,
        ).start()

    def _worker(self) -> None:
        while True:
            store = self.store()
            if store is not None:
                try:
                    store.rebuild()
                except Exception as exc:
                    self._record_error(exc)
            with self.lock:
                if self.pending:
                    self.pending = False
                    continue
                self.running = False
                return

    def _record_error(self, exc: BaseException) -> None:
        text = f"{type(exc).__name__}: {exc}"
        with self.lock:
            self.error_count += 1
            self.last_error = clip(text, 240)
            self.last_error_ref = digest_text(text)[:24]


__all__ = ["KnowledgeIndexer"]
