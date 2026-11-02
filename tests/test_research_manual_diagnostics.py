"""Local diagnostic failures neither change responses nor invent archive facts."""

import io
import json
from http.client import IncompleteRead
from urllib.error import HTTPError

import pytest

from tests.manual import kobold_diagnostics


@pytest.mark.parametrize("failure", [
    TimeoutError("diagnostic timed out"),
    HTTPError("http://localhost/check", 404, "not found", {}, None),
    IncompleteRead(b"partial"),
    json.JSONDecodeError("invalid diagnostic JSON", "x", 0),
])
def test_optional_native_diagnostic_records_failure_without_raising(monkeypatch, failure):
    def unavailable(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(kobold_diagnostics, "urlopen", unavailable)
    events = []
    kobold_diagnostics.record_native_generation("http://127.0.0.1:5001", events.append)
    assert len(events) == 1
    assert events[0]["event"] == "native_generation_diagnostic_failure"
    assert repr(failure) == events[0]["error"]


def test_native_diagnostic_preserves_the_raw_generation(monkeypatch):
    body = {"results": [{"text": "<tool_call>raw output</tool_call>"}]}
    monkeypatch.setattr(kobold_diagnostics, "urlopen", lambda *_args, **_kwargs: io.BytesIO(json.dumps(body).encode()))
    events = []
    kobold_diagnostics.record_native_generation("http://127.0.0.1:5001/", events.append)
    assert events == [{"event": "native_generation", "body": body}]
