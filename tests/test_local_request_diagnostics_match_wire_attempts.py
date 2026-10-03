"""Request observations correspond to bytes actually sent, including retries."""
import hashlib
import json
from unittest.mock import patch

import pytest

from codey.providers.local_openai import LocalOpenAIProvider


class Response:
    def __init__(self, raw):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, _limit):
        return self.raw


class ObservedProvider(LocalOpenAIProvider):
    def __init__(self):
        super().__init__("http://localhost:5001/v1", "fake", api_key="must-not-be-recorded")
        self.observations = []

    def _observe_http_attempt(self, *, attempt, data, phase, response_bytes, seconds):
        self.observations.append((attempt, phase, hashlib.sha256(data).hexdigest(), response_bytes))


def test_each_physical_retry_observes_identical_actual_request_bytes():
    provider = ObservedProvider()
    sent = []

    def transport(request, **_):
        sent.append(request.data)
        return Response(b"" if len(sent) == 1 else b'{"choices":[]}')

    with patch("urllib.request.urlopen", side_effect=transport), patch.object(provider, "_request_payload", wraps=provider._request_payload) as payload:
        assert provider._post_chat([{"role": "user", "content": "汉字"}]) == {"choices": []}
    payload.assert_called_once()
    digest = hashlib.sha256(sent[0]).hexdigest()
    assert sent[0] == sent[1]
    assert [(n, phase, h) for n, phase, h, _ in provider.observations] == [
        (1, "request", digest), (1, "retryable_error", digest), (2, "request", digest), (2, "response", digest),
    ]
    assert provider.api_key not in json.dumps(provider.observations)


def test_observer_failure_does_not_retry_successful_http_request():
    provider = ObservedProvider()
    with patch.object(provider, "_observe_http_attempt", side_effect=OSError("diagnostic disk full")), patch("urllib.request.urlopen", return_value=Response(b'{"ok":true}')) as transport:
        assert provider._post_chat([]) == {"ok": True}
    transport.assert_called_once()


def test_error_attempt_is_observed_once_without_changing_exception():
    provider = ObservedProvider()
    with patch("urllib.request.urlopen", return_value=Response(b"not JSON")), pytest.raises(RuntimeError, match="non-JSON"):
        provider._post_chat([])
    assert [row[1] for row in provider.observations] == ["request", "error"]
