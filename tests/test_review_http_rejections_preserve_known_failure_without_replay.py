"""Known API refusals retain HTTP facts through initial and repair Review sends."""
import io
import json
import urllib.error
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codey.app.review_service import ReviewSendUnknown, run_review_attempt
from codey.providers import api_transport
from codey.providers.api_provider import ApiProvider


@pytest.mark.parametrize("phase", ["initial", "repair"])
@pytest.mark.parametrize("status,error_type", [(403, "FreeTierError"), (429, "FreeUsageLimitError")])
def test_review_retains_complete_http_rejection_and_does_not_replay(tmp_path, monkeypatch, phase, status, error_type):
    (tmp_path / "app.py").write_text("def add(a, b): return a + b\n", encoding="utf8")
    attempts = []

    def request(req, timeout):
        attempts.append(json.loads(req.data))
        if phase == "repair" and len(attempts) == 1:
            from tests.test_api_generation_observations_no_replay import Response

            return Response(json.dumps({"status": "completed", "output": [
                {"type": "message", "role": "assistant", "content": [
                    {"type": "output_text", "text": "not a valid review"}]}]}).encode())
        body = json.dumps({"error": {"type": error_type, "message": "upstream access refusal"}}).encode()
        raise urllib.error.HTTPError(req.full_url, status, "rejected", {}, io.BytesIO(body))

    monkeypatch.setattr(api_transport, "open_request", request)
    reviewer = ApiProvider("http://fixture.test/v1", "fixture", api_protocol="openai-responses")
    reviewer.close = Mock(wraps=reviewer.close)
    with pytest.raises(api_transport.GenerationRejectedError) as raised:
        run_review_attempt(SimpleNamespace(state_home=None, emit=lambda _: None), session_id="s",
            project=str(tmp_path), task="review", writer_summary="", changes={"ok": True,
            "files": [{"path": "app.py"}], "diff": ""}, recent_log="", change_brief="",
            project_map="", verification_map="", review_impact_map="", execution_evidence="",
            reviewer_id="local", reviewer=reviewer, self_review=False)
    assert raised.value.status == status
    assert raised.value.error_type == error_type
    assert len(attempts) == (2 if phase == "repair" else 1)
    assert all(not payload.get("tools") for payload in attempts)
    reviewer.close.assert_called_once()


def test_actual_unknown_review_send_retains_unknown_error_and_never_replays(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf8")
    reviewer = SimpleNamespace(new_chat=lambda: None, close=Mock(),
        send=Mock(side_effect=api_transport.GenerationUnknownError("stream ended after submission")))
    with pytest.raises(ReviewSendUnknown, match="stream ended after submission"):
        run_review_attempt(SimpleNamespace(state_home=None, emit=lambda _: None), session_id="s",
            project=str(tmp_path), task="review", writer_summary="", changes={"ok": True,
            "files": [{"path": "app.py"}], "diff": ""}, recent_log="", change_brief="",
            project_map="", verification_map="", review_impact_map="", execution_evidence="",
            reviewer_id="local", reviewer=reviewer, self_review=False)
    reviewer.send.assert_called_once()
    reviewer.close.assert_called_once()
