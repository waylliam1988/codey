"""Every exchange releases structural resources when settlement fails."""

import pytest

from codey.providers.api_provider import ApiProvider
from codey.providers.api_request_policy import LocalRequestPolicy, PendingRequest


class _ExplodingSettle(LocalRequestPolicy):
    def settle(self, context, receipt):  # type: ignore[no-untyped-def]
        raise RuntimeError("settle failed")


class _ExplodingPending(_ExplodingSettle):
    def register(self, context):  # type: ignore[no-untyped-def]
        return PendingRequest(lambda: (_ for _ in ()).throw(RuntimeError("pending close failed")))


def test_settle_failure_still_releases_send_lock(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    provider = ApiProvider("http://fixture/v1", "m", request_policy=_ExplodingSettle())
    monkeypatch.setattr(
        "codey.providers.api_transport.generate",
        lambda *args, **kwargs: {"choices": [{"message": {"content": "ready"}, "finish_reason": "stop"}]},
    )
    with pytest.raises(RuntimeError, match="settle failed"):
        provider.send("hello")
    assert provider._send_lock.acquire(blocking=False)
    provider._send_lock.release()


def test_pending_close_failure_still_releases_send_lock(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    provider = ApiProvider("http://fixture/v1", "m", request_policy=_ExplodingPending())
    monkeypatch.setattr(
        "codey.providers.api_transport.generate",
        lambda *args, **kwargs: {"choices": [{"message": {"content": "ready"}, "finish_reason": "stop"}]},
    )
    with pytest.raises(RuntimeError):
        provider.send("hello")
    assert provider._send_lock.acquire(blocking=False)
    provider._send_lock.release()
