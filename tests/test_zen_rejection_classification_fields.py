"""Zen rejection classification fails closed for unknown provider errors."""

from codey.providers.api_transport import GenerationRejectedError
from codey.providers.zen.reliability import classify_rejection


def test_rejection_preserves_error_code_and_upstream_request_id():
    error = GenerationRejectedError(
        429,
        '{"error":{"type":"rate_limit","code":"too_many_requests","message":"busy",'
        '"request_id":"req-1"}}',
        "2",
        upstream_request_id="header-1",
    )
    result = classify_rejection(error)
    assert error.error_code == "too_many_requests"
    assert error.upstream_request_id == "header-1"
    assert result.retryable is True
    assert result.scope == "model"


def test_unknown_429_is_not_replayed():
    error = GenerationRejectedError(429, '{"error":{"message":"unknown"}}')
    assert classify_rejection(error).retryable is False


def test_explicit_unavailable_upstream_is_transient_but_other_server_errors_are_not():
    import time

    error = GenerationRejectedError(429, '{"error":{"type":"server_error",'
        '"message":"Upstream request failed: Endpoint is unavailable."}}', upstream_request_id="upstream-1")
    result = classify_rejection(error)
    assert result.retryable and result.scope == "model"
    assert result.not_before >= time.monotonic() + 1.8
    assert error.upstream_request_id == "upstream-1"
    for message in ("unknown", "Endpoint is unavailable", "quota exceeded"):
        other = GenerationRejectedError(429, '{"error":{"type":"server_error","message":"' + message + '"}}')
        assert classify_rejection(other).retryable is False
