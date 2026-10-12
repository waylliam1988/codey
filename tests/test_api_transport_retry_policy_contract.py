"""Retry decisions and submission classification belong to the request policy."""

import ssl
import urllib.request

import pytest

from codey.providers.api_http import ApiHttpSession, ConnectionKey
from codey.providers.api_provider import ApiProvider
from codey.providers.api_request_policy import LocalRequestPolicy, RetryDirective
from codey.providers.api_transport import (
    GenerationNotSentError,
    GenerationRejectedError,
    GenerationUnknownError,
    _open_pooled_request,
)


class _Policy(LocalRequestPolicy):
    def __init__(self) -> None:
        self.failures = 0

    def observe_failure(self, context, receipt, failure, budget):  # type: ignore[no-untyped-def]
        self.failures += 1
        return RetryDirective(False, reason="policy-owned")


def test_local_policy_override_controls_rejected_generation(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    policy = _Policy()
    provider = ApiProvider("http://fixture/v1", "m", request_policy=policy)
    monkeypatch.setattr(
        "codey.providers.api_transport.generate",
        lambda *args, **kwargs: (_ for _ in ()).throw(GenerationRejectedError(503, "busy")),
    )
    with pytest.raises(GenerationRejectedError):
        provider.send("hello")
    assert policy.failures == 1


def test_plain_503_is_not_automatically_retried(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    provider = ApiProvider("http://fixture/v1", "m")
    calls = 0

    def reject(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        raise GenerationRejectedError(503, "busy")

    monkeypatch.setattr("codey.providers.api_transport.generate", reject)
    with pytest.raises(GenerationRejectedError):
        provider.send("hello")
    assert calls == 1


def test_policy_can_retry_a_pre_submission_tls_eof_once(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class RetryPreSend(_Policy):
        def observe_failure(self, context, receipt, failure, budget):  # type: ignore[no-untyped-def]
            self.failures += 1
            return RetryDirective(self.failures == 1, reason="pre_submission_tls_eof")

    policy = RetryPreSend()
    provider = ApiProvider("http://fixture/v1", "m", request_policy=policy)
    calls = 0

    def generate(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise GenerationNotSentError("pre-submission TLS EOF")
        return {"choices": [{"message": {"content": "ready"}, "finish_reason": "stop"}]}

    monkeypatch.setattr("codey.providers.api_transport.generate", generate)
    assert provider.send("hello") == "ready"
    assert calls == 2


def test_pooled_tls_eof_after_generation_write_is_unknown() -> None:
    class Connection:
        def request(self, *_args, **_kwargs):
            self._codey_submission_started = True
            raise ssl.SSLEOFError("after write")

        def close(self):
            pass

    session = ApiHttpSession()
    key = ConnectionKey("http://fixture.test", "", "", "http")
    session.release_connection(key, Connection())
    request = urllib.request.Request("http://fixture.test/v1", data=b"{}", method="POST")
    with pytest.raises(GenerationUnknownError) as caught:
        _open_pooled_request(request, 1, control=type("Control", (), {
            "cancelled": False,
            "register_lease": lambda self, lease: None,
        })(), http_session=session)
    assert caught.value.reason_code == "post_submission_tls_eof"
