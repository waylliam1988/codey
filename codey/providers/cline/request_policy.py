"""Bounded account admission and retries inside the shared request lifecycle."""
from __future__ import annotations

import json
import threading
import time

from codey.providers.api_request_policy import (
    AttemptBudget,
    LocalRequestPolicy,
    PendingRequest,
    RequestContext,
    RequestLease,
    RetryDirective,
)
from codey.providers.api_transport import GenerationRejectedError
from codey.providers.cline.runtime import Runtime
from codey.providers.connection_controls import ConnectionActionError


def safe_error_detail(status: int, detail: str) -> str:
    invalid = "invalid request" in detail.lower()
    message = ("Upstream rejected this request." if invalid else
               "Cline authentication rejected." if status == 401 else
               "Cline free access unavailable." if status == 403 else
               "Cline rate limit reached." if status == 429 else "Cline service request failed.")
    return json.dumps({"error": {"message": message, "code": "upstream_invalid_request" if invalid else "service_rejection"}})


class ClineRequestPolicy(LocalRequestPolicy):
    max_attempts = 3

    def __init__(self, runtime: Runtime, epoch: str, model: str, capability_revision: str) -> None:
        self.runtime, self.epoch, self.model = runtime, epoch, model
        self.capability_revision = capability_revision
        self.versions: dict[str, int] = {}
        self.retried_auth: set[str] = set()
        self.lock = threading.Lock()

    def register(self, context: RequestContext) -> PendingRequest:
        capability = self.runtime.catalog.require(self.model, deadline=context.deadline, cancelled=context.cancelled)
        if capability.revision != self.capability_revision:
            raise ValueError("Cline model capability changed; select the model again.")
        with self.runtime.gate:
            record = self.runtime.auth.store.load()
            self.runtime.auth._check_record(record, self.epoch)
            if self.runtime.closed or self.runtime.blocked:
                raise ConnectionActionError("connection_blocked", "Cline connection is blocked; reconnect explicitly.", 409)
            self.runtime.active += 1

        def release() -> None:
            with self.runtime.gate:
                self.runtime.active -= 1
            with self.lock:
                self.versions.pop(context.logical_id, None)
                self.retried_auth.discard(context.logical_id)

        return PendingRequest(release)

    def acquire(self, pending: PendingRequest, context: RequestContext) -> RequestLease:
        if context.cancelled() or time.monotonic() >= context.deadline:
            raise TimeoutError("Cline request cancelled or timed out")
        return RequestLease(lambda: None)

    def remember_version(self, logical_id: str, version: int) -> None:
        with self.lock:
            self.versions[logical_id] = version

    def observe_failure(self, context: RequestContext, receipt: object, failure: BaseException,
                        budget: AttemptBudget) -> RetryDirective:
        if not isinstance(failure, GenerationRejectedError):
            return RetryDirective(False)
        if failure.status == 401 and context.logical_id not in self.retried_auth:
            self.retried_auth.add(context.logical_id)
            try:
                self.runtime.auth.credentials(context.deadline, context.cancelled, self.epoch,
                                              force_version=self.versions.get(context.logical_id, -1))
            except (RuntimeError, TimeoutError, ValueError):
                return RetryDirective(False)
            return RetryDirective(budget.used < budget.max_total, reason="authentication_refreshed")
        if failure.status == 429:
            delay = failure.retry_after if failure.retry_after is not None else 1.0
            when = time.monotonic() + min(30.0, max(0, delay))
            if delay > 30 or when >= context.deadline:
                return RetryDirective(False)
            return RetryDirective(budget.used < budget.max_total, when, "rate_limited")
        if failure.status == 401:
            with self.runtime.auth.lock:
                self.runtime.auth._state("reauth_required", "reauth_required")
        return RetryDirective(False)
