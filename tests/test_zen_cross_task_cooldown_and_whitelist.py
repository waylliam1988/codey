"""Zen cooldowns survive provider lifetimes and unknown errors fail closed."""

import time

from codey.providers.api_transport import GenerationRejectedError
from codey.providers.zen.reliability import (
    ZenResourcesRegistry,
    classify_rejection,
    make_control_scope,
)


def test_registry_retains_active_cooldown_after_last_provider_release():
    scope = make_control_scope("http://zen", "public", "r1")
    coordinator = ZenResourcesRegistry.get_or_create(scope)
    coordinator.publish_cooldown(scope, model_id="m", not_before=time.monotonic() + 30, reason="429")
    ZenResourcesRegistry.release(scope)


def test_last_provider_release_closes_idle_session_but_keeps_cooldown():
    scope = make_control_scope("http://zen-idle-close", "public", "r1")
    coordinator = ZenResourcesRegistry.get_or_create(scope)
    session = ZenResourcesRegistry.get_session(scope)
    connection = type("Connection", (), {"closed": False, "close": lambda self: setattr(self, "closed", True)})()
    from codey.providers.api_http import ConnectionKey

    session.release_connection(ConnectionKey("http://zen", "", "", "http"), connection)
    coordinator.publish_cooldown(scope, model_id="m", not_before=time.monotonic() + 30, reason="429")
    ZenResourcesRegistry.release(scope)
    assert connection.closed is True


def test_closed_session_rejects_late_return_without_repopulating_idle_pool():
    from codey.providers.api_http import ApiHttpSession, ConnectionKey

    session = ApiHttpSession()
    session.release()
    connection = type("Connection", (), {"closed": False, "close": lambda self: setattr(self, "closed", True)})()
    session.release_connection(ConnectionKey("http://late", "", "", "http"), connection)
    assert connection.closed is True


def test_expired_cooldown_survives_last_provider_release_for_a_single_probe():
    scope = make_control_scope("http://zen-expired", "public", "r1")
    coordinator = ZenResourcesRegistry.get_or_create(scope)
    coordinator.publish_cooldown(scope, model_id="m", not_before=time.monotonic() - 1, reason="429")
    ZenResourcesRegistry.release(scope)
    again = ZenResourcesRegistry.get_or_create(scope)
    assert again is coordinator
    pending = again.register(type("Context", (), {})())
    context = type("Context", (), {"deadline": time.monotonic() + 1, "cancelled": lambda self: False})()
    lease = again.acquire(pending, context, scope=scope, model_id="m")
    assert lease.receipt is not None
    lease.release()
    again.settle(scope=scope, model_id="m", receipt=lease.receipt, successful=True)
    pending.close()
    ZenResourcesRegistry.release(scope)


def test_unknown_rate_error_type_is_not_retryable():
    error = GenerationRejectedError(429, '{"error":{"type":"UnknownRateLimitError","code":"mystery"}}')
    assert classify_rejection(error).retryable is False


def test_only_confirmed_short_window_rate_limit_is_retryable():
    error = GenerationRejectedError(429, '{"error":{"type":"RateLimitError","code":"rate_limit"}}')
    result = classify_rejection(error)
    assert result.retryable is True
    assert result.kind == "transient"


def test_free_usage_and_generic_server_errors_are_not_retryable():
    for status, body in [
        (429, '{"error":{"type":"FreeUsageLimitError"}}'),
        (429, '{"error":{"type":"BlackUsageLimitError"}}'),
        (429, '{"error":{"type":"GoUsageLimitError"}}'),
        (503, '{"error":{"type":"ServerError"}}'),
        (503, '{"error":{"type":"UnknownRateLimitError"}}'),
    ]:
        assert classify_rejection(GenerationRejectedError(status, body)).retryable is False
