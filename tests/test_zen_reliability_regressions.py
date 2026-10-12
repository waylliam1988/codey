"""Zen cooldown monotonicity and probe ownership regressions."""

import time

from codey.providers.zen.reliability import ZenRequestCoordinator, make_control_scope


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
