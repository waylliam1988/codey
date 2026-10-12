"""Transport reconnects and body silence consume the shared bounded budget."""

import time

import pytest

from codey.providers.api_request_policy import AttemptBudget
from codey.providers.api_transport import AttemptControl, BoundedBodyReader, GenerationUnknownError


def test_tls_reconnect_cannot_bypass_attempt_budget():
    budget = AttemptBudget(1)
    assert budget.reserve("post")
    assert not budget.reserve("tls_reconnect")


def test_body_reader_enforces_byte_silence_deadline():
    class Source:
        def read(self, _size):
            time.sleep(0.03)
            return b"x"
    with pytest.raises(GenerationUnknownError):
        BoundedBodyReader(Source(), control=AttemptControl(), deadline=time.monotonic() + 1,
                          idle_timeout=0.01).read_chunk()
