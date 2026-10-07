"""Diagnostic observations match one physical generation and never cause replay."""
import hashlib
import json
from unittest.mock import patch

import pytest

from codey.providers.api_provider import ApiProvider
from codey.providers.api_transport import GenerationUnknownError


class Response:
    def __init__(self, raw):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, _limit):
        return self.raw


class ObservedProvider(ApiProvider):
    def __init__(self):
        super().__init__("http://model.test/v1", "fixture", api_key="must-not-be-recorded")
        self.observations = []

    def _observe_http_attempt(self, *, attempt, data, phase, response_bytes, seconds):
        self.observations.append((attempt, phase, hashlib.sha256(data).hexdigest(), response_bytes))


@pytest.mark.parametrize("raw", [b"", b"not JSON"])
def test_unknown_response_has_one_observed_attempt_and_never_consumes_second_reply(raw):
    provider = ObservedProvider()
    sent = []

    def transport(request, timeout):
        sent.append(request.data)
        return Response(raw if len(sent) == 1 else b'{"choices":[]}')

    with patch("codey.providers.api_transport.open_request", side_effect=transport), pytest.raises(GenerationUnknownError):
        provider._post_chat([{"role": "user", "content": "汉字"}])
    digest = hashlib.sha256(sent[0]).hexdigest()
    assert len(sent) == 1
    assert [(n, phase, h) for n, phase, h, _ in provider.observations] == [(1, "request", digest), (1, "unknown", digest)]
    assert provider.api_key not in json.dumps(provider.observations)


def test_observer_failure_does_not_retry_successful_http_request():
    provider = ObservedProvider()
    with patch.object(provider, "_observe_http_attempt", side_effect=OSError("diagnostic disk full")), patch("codey.providers.api_transport.open_request", return_value=Response(b'{"ok":true}')) as transport:
        assert provider._post_chat([]) == {"ok": True}
    transport.assert_called_once()
