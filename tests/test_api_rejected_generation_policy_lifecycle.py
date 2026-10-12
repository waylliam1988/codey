"""Explicit rejection policy: plain gateway failures are never replayed."""
import json
from io import BytesIO
from urllib.error import HTTPError

import pytest

from codey.providers import api_transport
from codey.providers.api_provider import ApiProvider


class Response(BytesIO):
    headers = {"Content-Type": "application/json"}


def test_plain_503_is_not_replayed_and_keeps_history_uncommitted(monkeypatch):
    requests, usage = [], []
    def open_request(request, timeout):
        requests.append(request.data)
        if len(requests) == 1:
            raise HTTPError(request.full_url, 503, "busy", {"Retry-After": "0"}, BytesIO(b'{"error":{"type":"server_error"}}'))
        return Response(json.dumps({"choices": [{"message": {"content": "ready"}, "finish_reason": "stop"}]}).encode())
    monkeypatch.setattr(api_transport, "open_request", open_request)
    provider = ApiProvider("http://local.test/v1", "fixture")
    provider.bind_usage("local", usage.append)
    with pytest.raises(api_transport.GenerationRejectedError):
        provider.send("hello")
    assert len(requests) == 1
    assert [r.outcome for r in usage] == ["rejected"]
    assert provider._messages == []


def test_plain_503_has_one_physical_attempt(monkeypatch):
    attempts = []
    def request(req, timeout):
        attempts.append(req)
        raise HTTPError(req.full_url, 503, "busy", {"Retry-After": "0"}, BytesIO(b'{}'))
    monkeypatch.setattr(api_transport, "open_request", request)
    provider = ApiProvider("http://local.test/v1", "fixture")
    with pytest.raises(api_transport.GenerationRejectedError):
        provider.send("hello")
    assert len(attempts) == 1
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


def test_cancellation_after_plain_rejection_does_not_create_retry(monkeypatch):
    attempts = []
    def request(req, timeout):
        attempts.append(req)
        raise HTTPError(req.full_url, 503, "busy", {"Retry-After": "1"}, BytesIO(b'{}'))
    monkeypatch.setattr(api_transport, "open_request", request)
    provider = ApiProvider("http://local.test/v1", "fixture")
    with pytest.raises(api_transport.GenerationRejectedError):
        provider.send("hello")
    provider.abandon_inflight()
    assert len(attempts) == 1
