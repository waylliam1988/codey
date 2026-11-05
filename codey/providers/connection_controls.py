"""Safe errors exposed by optional connection controls."""
from __future__ import annotations


class ConnectionActionError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400, *, pending_cleanup: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.code, self.status = code, status
        self.pending_cleanup = pending_cleanup
