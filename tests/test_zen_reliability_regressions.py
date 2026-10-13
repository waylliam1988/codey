"""Zen cooldown monotonicity and probe ownership regressions."""

import contextlib
import time

from codey.providers.zen.reliability import ZenRequestCoordinator, ZenRequestPolicy, make_control_scope


def test_short_retry_after_does_not_shorten_existing_cooldown() -> None:
    scope = make_control_scope("http://zen-cooldown", "public", "r1")
    coordinator = ZenRequestCoordinator()
    long_until = time.monotonic() + 60
    short_until = time.monotonic() + 1
    coordinator.publish_cooldown(scope, model_id="m", not_before=long_until, reason="long")
    coordinator.publish_cooldown(scope, model_id="m", not_before=short_until, reason="short")
    with coordinator._condition:
        until, _version, _reason = coordinator._cooldowns[(scope, "m")]
    assert until >= long_until


def test_old_probe_success_cannot_clear_newer_cooldown() -> None:
    scope = make_control_scope("http://zen-probe", "public", "r1")
    coordinator = ZenRequestCoordinator()
    coordinator.publish_cooldown(scope, model_id="m", not_before=time.monotonic() - 1, reason="first")
    pending = coordinator.register(type("Context", (), {})())
    context = type("Context", (), {"deadline": time.monotonic() + 1, "cancelled": lambda self: False})()
    lease = coordinator.acquire(pending, context, scope=scope, model_id="m")
    old_receipt = lease.receipt
    coordinator.publish_cooldown(scope, model_id="m", not_before=time.monotonic() + 30, reason="new")
    coordinator.settle(scope=scope, model_id="m", receipt=old_receipt, successful=True)
    assert coordinator.has_active_cooldown()
    lease.release()
    pending.close()


def test_successful_probe_settlement_clears_matching_cooldown() -> None:
    scope = make_control_scope("http://zen-success", "public", "r1")
    coordinator = ZenRequestCoordinator()
    policy = ZenRequestPolicy(coordinator, scope)
    coordinator.publish_cooldown(scope, model_id="m", not_before=time.monotonic() - 1, reason="429")
    pending = coordinator.register(type("Context", (), {})())
    context = type("Context", (), {"deadline": time.monotonic() + 1, "model_id": "m", "cancelled": lambda self: False})()
    lease = coordinator.acquire(pending, context, scope=scope, model_id="m")
    policy.settle(context, lease, successful=True)
    assert not coordinator.has_active_cooldown()
    lease.release()
    pending.close()


def test_probe_held_wait_uses_positive_timeout(monkeypatch) -> None:
    scope = make_control_scope("http://zen-wait", "public", "r1")
    coordinator = ZenRequestCoordinator(max_inflight=2)
    coordinator.publish_cooldown(scope, model_id="m", not_before=time.monotonic() - 1, reason="429")
    owner = coordinator.register(type("Context", (), {})())
    context = type("Context", (), {"deadline": time.monotonic() + 1, "cancelled": lambda self: False})()
    owner_lease = coordinator.acquire(owner, context, scope=scope, model_id="m")
    waiter = coordinator.register(type("Context", (), {})())
    waits: list[float] = []
    original_wait = coordinator._condition.wait
    monkeypatch.setattr(coordinator._condition, "wait", lambda timeout=None: (waits.append(timeout), original_wait(0.001))[1])
    waiter_context = type("Context", (), {"deadline": time.monotonic() + 0.02, "cancelled": lambda self: False})()
    with contextlib.suppress(TimeoutError):
        coordinator.acquire(waiter, waiter_context, scope=scope, model_id="m")
    assert waits and all(value is None or value > 0 for value in waits)
    owner_lease.release()
    owner.close()
    waiter.close()
