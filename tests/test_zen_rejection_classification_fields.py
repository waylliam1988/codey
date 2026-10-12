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
