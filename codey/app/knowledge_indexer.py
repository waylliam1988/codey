"""Single-flight knowledge index rebuild coordination."""

from __future__ import annotations

import threading
from typing import Callable


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
                except Exception:
                    pass
            with self.lock:
                if self.pending:
                    self.pending = False
                    continue
                self.running = False
                return


__all__ = ["KnowledgeIndexer"]

