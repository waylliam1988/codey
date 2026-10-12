"""ApiProvider registers one logical request and releases each HTTP lease."""

import json

from codey.providers import api_transport
from codey.providers.api_provider import ApiProvider
from codey.providers.api_request_policy import LocalRequestPolicy, PendingRequest, RequestLease


class RecordingPolicy(LocalRequestPolicy):
    def __init__(self):
        self.registered = 0
        self.closed = 0
        self.acquired = 0
        self.released = 0

    def register(self, context):
        self.registered += 1
        return PendingRequest(lambda: setattr(self, "closed", self.closed + 1))

    def acquire(self, pending, context):
        self.acquired += 1
        return RequestLease(lambda: setattr(self, "released", self.released + 1))


def test_provider_policy_registration_survives_decode_and_closes_once(monkeypatch):
    policy = RecordingPolicy()
    provider = ApiProvider("http://fixture", "m", request_policy=policy)
    body = json.dumps({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}).encode()
    monkeypatch.setattr(api_transport, "open_request", lambda request, timeout: type("R", (), {
        "headers": {"Content-Type": "application/json"},
        "read": lambda self, size=-1: b"" if getattr(self, "_done", False) else (setattr(self, "_done", True) or body),
        "__enter__": lambda self: self,
        "__exit__": lambda self, *args: None,
    })())
    assert provider.send("hello") == "ok"
    assert (policy.registered, policy.closed, policy.acquired, policy.released) == (1, 1, 1, 1)
