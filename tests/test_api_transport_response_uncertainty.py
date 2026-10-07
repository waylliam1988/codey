"""An accepted generation with an unusable response is never retried implicitly."""
import http.client

import pytest

from codey.providers.api_provider import ApiProvider


class BrokenResponse:
    def __init__(self, mode):
        self.mode = mode

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        if self.mode == "断流":
            raise http.client.IncompleteRead(b'{"choices":', 100)
        return b'{"choices":'


@pytest.mark.parametrize("mode", ["断流", "畸形JSON"])
def test_generation_with_unknown_response_is_sent_once(monkeypatch, mode):
    attempts = []

    def request(req, timeout):
        attempts.append(req)
        return BrokenResponse(mode)

    monkeypatch.setattr("codey.providers.api_transport.open_request", request)
    provider = ApiProvider("http://model.test/v1", "fixture")
    with pytest.raises(RuntimeError):
        provider.send("synthetic test")
    assert len(attempts) == 1
    assert provider._messages == []
