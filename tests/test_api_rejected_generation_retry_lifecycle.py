"""Explicit rejection retries preserve deadlines, cancellation and physical usage."""
import json
from io import BytesIO
from urllib.error import HTTPError

import pytest

from codey.providers import api_transport
from codey.providers.api_provider import ApiProvider


class Response(BytesIO):
    headers = {"Content-Type": "application/json"}


def test_complete_503_retries_identical_payload_and_records_each_physical_attempt(monkeypatch):
    requests, usage = [], []
    def open_request(request, timeout):
        requests.append(request.data)
        if len(requests) == 1:
            raise HTTPError(request.full_url, 503, "busy", {"Retry-After": "0"}, BytesIO(b'{"error":{"type":"server_error"}}'))
        return Response(json.dumps({"choices": [{"message": {"content": "ready"}, "finish_reason": "stop"}]}).encode())
    monkeypatch.setattr(api_transport, "open_request", open_request)
    provider = ApiProvider("http://local.test/v1", "fixture")
    provider.bind_usage("local", usage.append)
    assert provider.send("hello") == "ready"
    assert len(requests) == 2 and requests[0] == requests[1]
    assert [r.outcome for r in usage] == ["rejected", "response"]
    assert len({r.exchange_id for r in usage}) == 2
    assert len([m for m in provider._messages if m.get("role") == "user"]) == 1


def test_503_retries_are_bounded_and_keep_uncommitted_history(monkeypatch):
    attempts = []
    def request(req, timeout):
        attempts.append(req)
        raise HTTPError(req.full_url, 503, "busy", {"Retry-After": "0"}, BytesIO(b'{}'))
    monkeypatch.setattr(api_transport, "open_request", request)
    provider = ApiProvider("http://local.test/v1", "fixture")
    with pytest.raises(api_transport.GenerationRejectedError):
        provider.send("hello")
    assert len(attempts) == 3
    assert provider._messages == []


def test_retry_after_beyond_remaining_deadline_does_not_resubmit(monkeypatch):
    attempts = []
    def request(req, timeout):
        attempts.append(req)
        raise HTTPError(req.full_url, 503, "busy", {"Retry-After": "60"}, BytesIO(b'{}'))
    monkeypatch.setattr(api_transport, "open_request", request)
    provider = ApiProvider("http://local.test/v1", "fixture", timeout=.1)
    with pytest.raises(api_transport.GenerationRejectedError):
        provider.send("hello")
    assert len(attempts) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 500, 502, 504])
def test_other_http_failures_are_not_implicitly_replayed(monkeypatch, status):
    attempts = []
    def request(req, timeout):
        attempts.append(req)
        raise HTTPError(req.full_url, status, "error", {}, BytesIO(b'{}'))
    monkeypatch.setattr(api_transport, "open_request", request)
    with pytest.raises(api_transport.GenerationRejectedError):
        ApiProvider("http://local.test/v1", "fixture").send("hello")
    assert len(attempts) == 1


def test_cancellation_during_rejection_backoff_prevents_a_second_request(monkeypatch):
    import threading

    observed = threading.Event()
    attempts = []
    def request(req, timeout):
        attempts.append(req)
        observed.set()
        raise HTTPError(req.full_url, 503, "busy", {"Retry-After": "1"}, BytesIO(b'{}'))
    monkeypatch.setattr(api_transport, "open_request", request)
    provider = ApiProvider("http://local.test/v1", "fixture")
    errors = []
    def send():
        try:
            provider.send("hello")
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=send)
    thread.start()
    assert observed.wait(1)
    provider.abandon_inflight()
    thread.join(1)
    assert not thread.is_alive() and len(attempts) == 1
    assert len(errors) == 1 and isinstance(errors[0], api_transport.GenerationNotSentError)
