"""Structured upstream rejection fields survive diagnostic sanitization."""

from codey.providers.diagnostics import ProviderFailure


def test_upstream_error_code_and_request_id_are_projected():
    failure = ProviderFailure("m", "send", "https://example.test", "error", "failed", "now",
                              facts={"http_status": 429, "service_error_code": "quota_exceeded",
                                     "upstream_request_id": "req-123"})
    assert failure.facts["service_error_code"] == "quota_exceeded"
    assert failure.facts["upstream_request_id"] == "req-123"
