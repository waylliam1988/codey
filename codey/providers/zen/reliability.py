"""Zen-specific admission, cooldown and rejection classification."""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Literal

from codey.providers.api_http import ApiHttpSession
from codey.providers.api_request_policy import (
    AttemptBudget,
    PendingRequest,
    RequestContext,
    RequestLease,
    RequestQueueFull,
    RetryDirective,
)
from codey.providers.api_transport import GenerationRejectedError

Scope = tuple[str, str, str]


@dataclass(frozen=True)
class ControlScope:
    base_url: str
    auth_identity: str
    agreement_revision: str


def make_control_scope(base_url: str, auth_identity: str, agreement_revision: str) -> ControlScope:
    return ControlScope(base_url.rstrip("/"), auth_identity, agreement_revision)


@dataclass(frozen=True)
class ZenRejection:
    kind: Literal["transient", "quota", "eligibility", "unknown"]
    scope: Literal["model", "identity", "session", "unknown"]
    retryable: bool
    not_before: float | None = None


def classify_rejection(error: GenerationRejectedError) -> ZenRejection:
    kind = (error.error_type or "").lower()
    code = (error.error_code or "").lower()
    # These names are the partner's explicit terminal usage/eligibility errors.
    # Keep this list closed: a 429 alone does not prove a short retry window.
    if kind in {"freeusagelimiterror", "blackusagelimiterror", "gousagelimiterror"}:
        return ZenRejection("quota", "identity", False)
    if code in {"quota_exceeded", "daily_limit", "free_tier_limit", "monthly_limit"}:
        return ZenRejection("quota", "identity", False)
    if kind in {"autherror", "creditserror", "monthlylimiterror", "userlimiterror",
                "modelerror", "regionerror", "datapolicyerror"} or error.status in {401, 403}:
        return ZenRejection("eligibility", "identity", False)
    # OpenCode's key limiter is the only confirmed short-window rejection.
    # FreeUsageLimitError is deliberately handled above as a daily/lifetime quota.
    if error.status == 429 and kind in {"ratelimiterror", "rate_limit"} and code in {"", "rate_limit", "too_many_requests"}:
        return ZenRejection("transient", "model", True,
                            time.monotonic() + (error.retry_after or 0.25))
    return ZenRejection("unknown", "unknown", False)


class ZenRequestCoordinator:
    def __init__(self, *, max_inflight: int = 3, max_pending: int = 8) -> None:
        self.max_inflight = max_inflight
        self.max_pending = max_pending
        self._condition = threading.Condition()
        self._pending: set[int] = set()
        self._inflight = 0
        self._cooldowns: dict[tuple[ControlScope, str], tuple[float, int, str]] = {}
        self._probes: dict[tuple[ControlScope, str], tuple[int, int]] = {}
        self._next_id = 0
        self._waiter_seq = 0
        self._waiters: deque[int] = deque()
        self._waiter_meta: dict[int, tuple[ControlScope, str]] = {}

    @property
    def pending_count(self) -> int:
        with self._condition:
            return len(self._pending)

    def has_active_cooldown(self) -> bool:
        with self._condition:
            return bool(self._cooldowns)

    def register(self, context: RequestContext) -> PendingRequest:
        with self._condition:
            if len(self._pending) >= self.max_pending:
                raise RequestQueueFull("Zen request queue is full")
            self._next_id += 1
            ident = self._next_id
            self._pending.add(ident)
        return PendingRequest(lambda: self._remove(ident))

    def _remove(self, ident: int) -> None:
        with self._condition:
            self._pending.discard(ident)
            for key, claim in list(self._probes.items()):
                if claim[0] == ident:
                    self._probes.pop(key, None)
            self._condition.notify_all()

    def acquire(self, pending: PendingRequest, context: RequestContext, *, scope: ControlScope,
                model_id: str, timeout: float | None = None) -> RequestLease:
        end = min(context.deadline, time.monotonic() + timeout) if timeout is not None else context.deadline
        with self._condition:
            self._waiter_seq += 1
            ticket = self._waiter_seq
            self._waiters.append(ticket)
            key = (scope, model_id)
            self._waiter_meta[ticket] = key
            while True:
                if context.cancelled():
                    self._waiters.remove(ticket)
                    self._waiter_meta.pop(ticket, None)
                    self._condition.notify_all()
                    raise RuntimeError("request cancelled while waiting for Zen request capacity")
                now = time.monotonic()
                cooldown = self._cooldowns.get((scope, model_id))
                blocked_until = cooldown[0] if cooldown else 0.0
                if now >= end:
                    self._waiters.remove(ticket)
                    self._waiter_meta.pop(ticket, None)
                    self._condition.notify_all()
                    raise TimeoutError("Zen request admission timed out")
                probe_blocked = cooldown is not None and now >= blocked_until and key in self._probes
                eligible = None
                if self._inflight < self.max_inflight:
                    for queued in self._waiters:
                        queued_key = self._waiter_meta.get(queued)
                        if queued_key is None:
                            continue
                        queued_cooldown = self._cooldowns.get(queued_key)
                        queued_until = queued_cooldown[0] if queued_cooldown else 0.0
                        queued_probe = queued_cooldown is not None and now >= queued_until and queued_key in self._probes
                        if now >= queued_until and not queued_probe:
                            eligible = queued
                            break
                at_head = ticket == eligible
                if at_head and now >= blocked_until and not probe_blocked and self._inflight < self.max_inflight:
                    self._waiters.remove(ticket)
                    self._waiter_meta.pop(ticket, None)
                    if cooldown is not None:
                        self._probes[key] = (id(pending), cooldown[1])
                    self._inflight += 1
                    lease = RequestLease(self._release)
                    lease.receipt = self._probes.get(key)
                    return lease
                self._condition.wait(min(0.05, max(0.001, min(end, blocked_until or end) - now)))

    def _release(self) -> None:
        with self._condition:
            self._inflight = max(0, self._inflight - 1)
            self._condition.notify_all()

    def publish_cooldown(self, scope: ControlScope, *, model_id: str, not_before: float, reason: str) -> int:
        with self._condition:
            old = self._cooldowns.get((scope, model_id))
            version = (old[1] + 1) if old else 1
            effective = max(old[0], not_before) if old is not None else not_before
            self._cooldowns[(scope, model_id)] = (effective, version, reason)
            self._probes.pop((scope, model_id), None)
            self._condition.notify_all()
            return version

    def observe_success(self, pending: PendingRequest, *, scope: ControlScope, model_id: str) -> None:
        # Success does not clear cooldown: a late response must never erase a newer 429.
        with self._condition:
            claim = self._probes.get((scope, model_id))
            if claim is None or claim[0] == id(pending):
                self._probes.pop((scope, model_id), None)
            self._condition.notify_all()

    def settle(self, *, scope: ControlScope, model_id: str, receipt: object | None = None,
               successful: bool = False) -> None:
        with self._condition:
            claim = self._probes.get((scope, model_id))
            if claim is not None and receipt is not None and receipt == claim:
                self._probes.pop((scope, model_id), None)
                if successful:
                    self._cooldowns.pop((scope, model_id), None)
            self._condition.notify_all()


class ZenRequestPolicy:
    """Adapter consumed by the shared API provider without importing Zen there."""

    def __init__(self, coordinator: ZenRequestCoordinator, scope: ControlScope) -> None:
        self.coordinator = coordinator
        self.scope = scope

    def register(self, context: RequestContext) -> PendingRequest:
        return self.coordinator.register(context)

    def acquire(self, pending: PendingRequest, context: RequestContext) -> RequestLease:
        return self.coordinator.acquire(pending, context, scope=self.scope, model_id=context.model_id)

    def observe_failure(self, context: RequestContext, receipt: object, failure: BaseException,
                        budget: AttemptBudget) -> RetryDirective:
        if not isinstance(failure, GenerationRejectedError):
            return RetryDirective(False, reason=str(failure))
        result = classify_rejection(failure)
        if not result.retryable:
            self.coordinator.settle(scope=self.scope, model_id=context.model_id, receipt=getattr(receipt, "receipt", None))
            return RetryDirective(False, reason=result.kind)
        if result.not_before is not None:
            self.coordinator.publish_cooldown(self.scope, model_id=context.model_id,
                                              not_before=result.not_before, reason=result.kind)
        return RetryDirective(True, result.not_before, result.kind)

    def observe_success(self, context: RequestContext, receipt: object) -> None:
        self.coordinator.settle(scope=self.scope, model_id=context.model_id,
                                receipt=getattr(receipt, "receipt", None), successful=True)

    def settle(self, context: RequestContext, receipt: object) -> None:
        self.coordinator.settle(scope=self.scope, model_id=context.model_id, receipt=getattr(receipt, "receipt", None))


class ZenResourcesRegistry:
    """Process-local shared coordinators with explicit retain/release."""

    _lock = threading.Lock()
    _resources: dict[ControlScope, tuple[ZenRequestCoordinator, int]] = {}
    _sessions: dict[ControlScope, ApiHttpSession] = {}

    @classmethod
    def get_or_create(cls, scope: ControlScope) -> ZenRequestCoordinator:
        with cls._lock:
            current = cls._resources.get(scope)
            if current is None:
                current = (ZenRequestCoordinator(), 0)
                cls._sessions[scope] = ApiHttpSession()
            cls._resources[scope] = (current[0], current[1] + 1)
            return current[0]

    @classmethod
    def get_session(cls, scope: ControlScope) -> ApiHttpSession:
        with cls._lock:
            if scope not in cls._resources:
                cls._resources[scope] = (ZenRequestCoordinator(), 0)
            session = cls._sessions.get(scope)
            if session is None:
                session = ApiHttpSession()
                cls._sessions[scope] = session
            return session

    @classmethod
    def release(cls, scope: ControlScope) -> None:
        session: ApiHttpSession | None = None
        with cls._lock:
            current = cls._resources.get(scope)
            if current is None:
                return
            if current[1] <= 1:
                if current[0].has_active_cooldown():
                    cls._resources[scope] = (current[0], 0)
                else:
                    cls._resources.pop(scope, None)
                session = cls._sessions.pop(scope, None)
            else:
                cls._resources[scope] = (current[0], current[1] - 1)
        if session is not None:
            session.release()
