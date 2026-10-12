"""Request policy lifecycle is bounded and shared across logical requests."""

import time

import pytest

from codey.providers.api_request_policy import (
    AttemptBudget,
    RequestContext,
    RequestLease,
    RequestQueueFull,
)
from codey.providers.zen.reliability import ZenRequestCoordinator, make_control_scope


def _context(model: str = "m") -> RequestContext:
    return RequestContext("http://zen", model, "openai-responses", "conversation", "id", time.monotonic() + 2)


def test_attempt_budget_counts_connection_and_post_attempts_once():
    budget = AttemptBudget(max_total=3)
    assert budget.reserve("connect")
    assert budget.reserve("post")
    assert budget.reserve("429")
    assert not budget.reserve("post")
    assert budget.used == 3


def test_zen_pending_limit_rejects_before_network_and_close_is_idempotent():
    coordinator = ZenRequestCoordinator(max_inflight=1, max_pending=1)
    first = coordinator.register(_context())
    with pytest.raises(RequestQueueFull):
        coordinator.register(_context("other"))
    first.close()
    first.close()
    assert coordinator.pending_count == 0


def test_cooldown_allows_one_probe_and_old_success_cannot_clear_new_version():
    coordinator = ZenRequestCoordinator(max_inflight=2, max_pending=4)
    scope = make_control_scope("http://zen", "public", "r1")
    one = coordinator.register(_context())
    two = coordinator.register(_context())
    coordinator.publish_cooldown(scope, model_id="m", not_before=time.monotonic() + 0.05, reason="429")
    with pytest.raises(TimeoutError):
        coordinator.acquire(one, _context(), scope=scope, model_id="m", timeout=0.01)
    time.sleep(0.06)
    lease = coordinator.acquire(one, _context(), scope=scope, model_id="m", timeout=0.2)
    assert isinstance(lease, RequestLease)
    coordinator.publish_cooldown(scope, model_id="m", not_before=time.monotonic() + 1, reason="new")
    lease.release()
    coordinator.observe_success(one, scope=scope, model_id="m")
    with pytest.raises(TimeoutError):
        coordinator.acquire(two, _context(), scope=scope, model_id="m", timeout=0.01)
    one.close()
    two.close()


def test_probe_is_settled_after_success_and_next_request_can_send():
    coordinator = ZenRequestCoordinator(max_inflight=1, max_pending=3)
    scope = make_control_scope("http://zen", "public", "r1")
    coordinator.publish_cooldown(scope, model_id="m", not_before=time.monotonic(), reason="429")
    first = coordinator.register(_context())
    lease = coordinator.acquire(first, _context(), scope=scope, model_id="m", timeout=0.1)
    lease.release()
    coordinator.observe_success(first, scope=scope, model_id="m")
    second = coordinator.register(_context())
    assert coordinator.acquire(second, _context(), scope=scope, model_id="m", timeout=0.1)
    first.close()
    second.close()


def test_old_probe_settlement_cannot_clear_new_probe_claim():
    coordinator = ZenRequestCoordinator(max_inflight=2, max_pending=4)
    scope = make_control_scope("http://zen", "public", "r1")
    coordinator.publish_cooldown(scope, model_id="m", not_before=time.monotonic(), reason="429")
    old = coordinator.register(_context())
    old_lease = coordinator.acquire(old, _context(), scope=scope, model_id="m", timeout=0.1)
    old_lease.release()
    coordinator.settle(scope=scope, model_id="m", receipt=old_lease.receipt)
    new = coordinator.register(_context())
    new_lease = coordinator.acquire(new, _context(), scope=scope, model_id="m", timeout=0.1)
    coordinator.settle(scope=scope, model_id="m", receipt=old_lease.receipt)
    third = coordinator.register(_context())
    with pytest.raises(TimeoutError):
        coordinator.acquire(third, _context(), scope=scope, model_id="m", timeout=0.01)
    new_lease.release()
    old.close()
    new.close()
    third.close()


def test_unowned_settlement_cannot_clear_probe_or_cooldown():
    coordinator = ZenRequestCoordinator(max_inflight=2, max_pending=4)
    scope = make_control_scope("http://zen", "public", "r1")
    coordinator.publish_cooldown(scope, model_id="m", not_before=time.monotonic(), reason="429")
    pending = coordinator.register(_context())
    lease = coordinator.acquire(pending, _context(), scope=scope, model_id="m", timeout=0.1)
    coordinator.settle(scope=scope, model_id="m", receipt=None)
    other = coordinator.register(_context())
    with pytest.raises(TimeoutError):
        coordinator.acquire(other, _context(), scope=scope, model_id="m", timeout=0.01)
    lease.release()
    pending.close()
    other.close()


def test_successful_probe_restores_normal_concurrency():
    coordinator = ZenRequestCoordinator(max_inflight=2, max_pending=4)
    scope = make_control_scope("http://zen", "public", "r1")
    coordinator.publish_cooldown(scope, model_id="m", not_before=time.monotonic(), reason="429")
    first = coordinator.register(_context())
    lease = coordinator.acquire(first, _context(), scope=scope, model_id="m", timeout=0.1)
    lease.release()
    coordinator.settle(scope=scope, model_id="m", receipt=lease.receipt, successful=True)
    a = coordinator.register(_context())
    b = coordinator.register(_context())
    la = coordinator.acquire(a, _context(), scope=scope, model_id="m", timeout=0.1)
    lb = coordinator.acquire(b, _context(), scope=scope, model_id="m", timeout=0.1)
    la.release()
    lb.release()
    first.close()
    a.close()
    b.close()


def test_distinct_model_is_not_blocked_by_other_model_cooldown():
    coordinator = ZenRequestCoordinator(max_inflight=1, max_pending=3)
    scope = make_control_scope("http://zen", "public", "r1")
    coordinator.publish_cooldown(scope, model_id="m", not_before=time.monotonic() + 1, reason="429")
    pending = coordinator.register(_context("other"))
    lease = coordinator.acquire(pending, _context("other"), scope=scope, model_id="other", timeout=0.1)
    lease.release()
    pending.close()
