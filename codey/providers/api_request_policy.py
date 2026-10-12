"""Small, provider-neutral request admission and retry primitives.

The policy owns logical-request accounting.  Transport attempts are deliberately
not represented as pending requests, so a retry cannot bypass queue limits.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class RequestContext:
    base_url: str
    model_id: str
    protocol: str
    purpose: str
    logical_id: str
    deadline: float
    cancelled: Any = lambda: False


@dataclass
class AttemptBudget:
    max_total: int
    used: int = 0

    def reserve(self, _kind: str) -> bool:
        if self.used >= self.max_total:
            return False
        self.used += 1
        return True


class RequestQueueFull(RuntimeError):
    pass


class RequestLease:
    def __init__(self, release: Any) -> None:
        self._release_fn = release
        self._released = False
        self._lock = threading.Lock()
        self.receipt: object | None = None

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._release_fn()

    def __enter__(self) -> RequestLease:
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class PendingRequest:
    def __init__(self, close: Any) -> None:
        self._close_fn = close
        self._closed = False
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._close_fn()


@dataclass(frozen=True)
class RetryDirective:
    retry: bool
    not_before: float | None = None
    reason: str = ""


class RequestPolicy(Protocol):
    def register(self, context: RequestContext) -> PendingRequest: ...
    def acquire(self, pending: PendingRequest, context: RequestContext) -> RequestLease: ...
    def observe_failure(self, context: RequestContext, receipt: object, failure: BaseException,
                        budget: AttemptBudget) -> RetryDirective: ...
    def observe_success(self, context: RequestContext, receipt: object) -> None: ...
    def settle(self, context: RequestContext, receipt: object) -> None: ...


def wait_until(when: float, context: RequestContext, *, interval: float = 0.05) -> None:
    """Wait interruptibly until a retry is eligible or raise a bounded error."""
    while True:
        if context.cancelled():
            raise RuntimeError("request cancelled while waiting to retry")
        remaining = min(when - time.monotonic(), context.deadline - time.monotonic())
        if remaining <= 0:
            if time.monotonic() >= context.deadline:
                raise TimeoutError("request deadline exceeded while waiting to retry")
            return
        time.sleep(min(interval, remaining))


class LocalRequestPolicy:
    """Default policy: no shared admission and no policy-owned retries."""
    max_attempts = 3
    def register(self, context: RequestContext) -> PendingRequest:
        return PendingRequest(lambda: None)

    def acquire(self, pending: PendingRequest, context: RequestContext) -> RequestLease:
        if context.cancelled():
            raise RuntimeError("request cancelled before send")
        return RequestLease(lambda: None)

    def observe_failure(self, context: RequestContext, receipt: object, failure: BaseException,
                        budget: AttemptBudget) -> RetryDirective:
        if getattr(failure, "reason_code", "") == "pre_submission_tls_eof" and budget.used < 2:
            return RetryDirective(True, reason="pre_submission_tls_eof")
        return RetryDirective(False, reason=str(failure))

    def observe_success(self, context: RequestContext, receipt: object) -> None:
        return None

    def settle(self, context: RequestContext, receipt: object) -> None:
        return None
