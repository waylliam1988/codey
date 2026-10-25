"""Session-local tool admission; execution facts remain in kernel receipts."""
from __future__ import annotations

import threading
from collections import deque
from collections.abc import Generator
from contextlib import contextmanager

from codey.runtime.core import cancellation


class ToolDispatch:
    """Admit parallel readers together and every other tool exclusively, in order."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._waiting: deque[object] = deque()
        self._readers = 0
        self._writer = False

    @contextmanager
    def admit(self, *, parallel: bool) -> Generator[None, None, None]:
        ticket = object()
        with self._condition:
            self._waiting.append(ticket)
            try:
                while (self._waiting[0] is not ticket or self._writer
                       or (not parallel and self._readers)):
                    cancellation.check()
                    self._condition.wait(cancellation.POLL_INTERVAL)
                cancellation.check()
            except BaseException:
                self._waiting.remove(ticket)
                self._condition.notify_all()
                raise
            self._waiting.popleft()
            if parallel:
                self._readers += 1
            else:
                self._writer = True
            self._condition.notify_all()
        try:
            yield
        finally:
            with self._condition:
                if parallel:
                    self._readers -= 1
                else:
                    self._writer = False
                self._condition.notify_all()
