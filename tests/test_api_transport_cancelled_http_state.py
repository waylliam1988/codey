"""Cancellation can reset HTTPConnection state before getresponse runs."""
import http.client
import urllib.request

import pytest

from codey.providers import api_transport
from tests.test_api_generation_observations_no_replay import ObservedProvider


@pytest.mark.parametrize('submission', ['none', 'started', 'possible'])
def test_cancelled_worker_http_state_error_keeps_submission_boundary(monkeypatch, submission):
    control = api_transport.AttemptControl()
    calls = []
    error = http.client.ResponseNotReady('Idle')
    def open_sync(request, timeout, **kwargs):
        calls.append(request)
        if submission == 'started':
            control.mark_submission_started()
        elif submission == 'possible':
            control.mark_submission_possible()
        control.cancel()
        raise error
    monkeypatch.setattr(api_transport, '_open_request_sync', open_sync)
    expected = api_transport.GenerationNotSentError if submission == 'none' else api_transport.GenerationUnknownError
    with pytest.raises(expected) as caught:
        api_transport.open_request(urllib.request.Request('http://model.test/v1', data=b'{}'), 2, control=control)
    assert caught.value.__cause__ is error and len(calls) == 1


def test_cancelled_submitted_http_state_error_is_observed_unknown_without_replay(monkeypatch):
    calls = []
    def open_sync(request, timeout, *, control, **kwargs):
        calls.append(request)
        control.mark_submission_started()
        control.cancel()
        raise http.client.ResponseNotReady('Idle')
    monkeypatch.setattr(api_transport, '_open_request_sync', open_sync)
    provider = ObservedProvider()
    try:
        with pytest.raises(api_transport.GenerationUnknownError):
            provider._generate([{'role': 'user', 'content': 'hello'}])
        assert len(calls) == 1
        assert [row[1] for row in provider.observations] == ['request', 'unknown']
        assert not provider.has_transport_history
    finally:
        provider.close()


def test_uncancelled_worker_http_state_error_is_not_hidden(monkeypatch):
    error = http.client.ResponseNotReady('Idle')
    def open_sync(*args, **kwargs):
        raise error
    monkeypatch.setattr(api_transport, '_open_request_sync', open_sync)
    with pytest.raises(http.client.ResponseNotReady) as caught:
        api_transport.open_request(urllib.request.Request('http://model.test/v1', data=b'{}'), 2,
                                   control=api_transport.AttemptControl())
    assert caught.value is error
