"""Explicit plain-request refusals inform later review selection, never replay."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from codey.providers import api_connections, api_transport
from codey.providers.base import AssistantTurn
from codey.providers.zen.connection import ZenProvider
from codey.runtime.core.api_selection import ApiRunSelection


def test_plain_access_refusal_is_scoped_persistent_and_expires(tmp_path, monkeypatch):
    from codey.providers.zen import access

    monkeypatch.setattr(access.time, "time", lambda: 1000.0)
    observations = access.ZenAccessObservations(tmp_path, "agreement")
    observations.record_plain_refusal("restricted", "openai-completions")
    observations.record_plain_success("confirmed", "openai-completions")
    restored = access.ZenAccessObservations(tmp_path, "agreement")
    assert restored.plain_refused("restricted", "openai-completions")
    assert not restored.plain_refused("restricted", "openai-responses")
    assert not restored.plain_refused("new-model", "openai-completions")
    assert restored.plain_access("confirmed", "openai-completions") is True
    assert restored.plain_access("new-model", "openai-completions") is None
    assert not access.ZenAccessObservations(tmp_path, "new-agreement").plain_refused("restricted", "openai-completions")
    monkeypatch.setattr(access.time, "time", lambda: 1000.0 + access.OBSERVATION_TTL + 1)
    assert not restored.plain_refused("restricted", "openai-completions")
    assert restored.plain_access("confirmed", "openai-completions") is None


def test_review_selection_skips_observed_refusal_without_changing_writer(monkeypatch):
    writer = ApiRunSelection("zen", "agreement", "writer", "openai-responses", True)
    connection = SimpleNamespace(model_payload=lambda: {"models": [
        {"id": "restricted", "review_eligible": False}, {"id": "writer"}, {"id": "available"}]},
        capture_selection=lambda payload: replace(writer, model_id=payload["model"]))
    monkeypatch.setattr(api_connections, "connection_for", lambda _: connection)
    assert api_connections.capture_reviewer_selection(writer).model_id == "available"
    assert writer.model_id == "writer"


def test_review_selection_prefers_recent_confirmed_access_over_unknown_model(monkeypatch):
    writer = ApiRunSelection("zen", "agreement", "writer", "openai-responses", True)
    connection = SimpleNamespace(model_payload=lambda: {"models": [
        {"id": "unknown", "review_eligible": None}, {"id": "confirmed", "review_eligible": True}]},
        capture_selection=lambda payload: replace(writer, model_id=payload["model"]))
    monkeypatch.setattr(api_connections, "connection_for", lambda _: connection)
    assert api_connections.capture_reviewer_selection(writer).model_id == "confirmed"


def test_successful_text_channel_records_access_once_with_unavailable_transport_declarations():
    calls, observations = [], []
    runtime = SimpleNamespace(send_turn=lambda text, tools, timeout=None: calls.append((text, tools, timeout)) or AssistantTurn(text='review'))
    provider = ZenProvider(runtime, on_plain_succeeded=lambda: observations.append(True))
    assert provider.send("Review", timeout=1) == 'review'
    assert len(calls) == 1 and calls[0][0].endswith("\n\nReview")
    assert {t.name for t in calls[0][1]} == {"read", "shell"}
    assert 0 < calls[0][2] <= 1 and observations == [True]


@pytest.mark.parametrize("failure,observed", [("refused", True), ("unknown", False), ("other-refusal", False)])
def test_zen_plain_request_records_only_explicit_free_tier_refusal_without_replay(failure, observed):
    calls, refusals = [], []
    error = (api_transport.GenerationUnknownError("unknown") if failure == "unknown" else
             api_transport.GenerationRejectedError(403, '{"error":{"type":"FreeTierError"}}' if failure == "refused" else 'other'))

    def send(*args, **kwargs):
        calls.append(args)
        raise error

    provider = ZenProvider(SimpleNamespace(send_turn=send), on_plain_refused=lambda: refusals.append(True))
    with pytest.raises(type(error)):
        provider.send("Review the provided diff", timeout=1)
    assert len(calls) == 1
    assert bool(refusals) == observed
