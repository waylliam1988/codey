"""Changed experiments fail before touching the model; whole answers stay unreviewed."""

import json
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from tests.manual import research_experiment_identity as identity
from tests.manual.persistent_research_local import whole_answer_observed
from tests.manual.research_framework_comparison import InferenceProxy


def test_matrix_and_proxy_reject_changed_source_before_inference(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "source_identity", lambda: {"runtime_id": "original"})
    identity.freeze_experiment(tmp_path)
    identity.verify_experiment(tmp_path)
    monkeypatch.setattr(identity, "source_identity", lambda: {"runtime_id": "changed"})
    with pytest.raises(RuntimeError, match="source changed"):
        identity.freeze_experiment(tmp_path)
    wire = tmp_path / "wire.jsonl"
    with InferenceProxy(wire, 74, upstream="http://invalid.invalid", identity_root=tmp_path) as proxy:
        request = Request(proxy + "/v1/chat/completions", data=b'{"messages":[]}',
                          headers={"Content-Type": "application/json"})
        with pytest.raises(HTTPError) as caught:
            urlopen(request, timeout=5)
        assert caught.value.code == 409
    assert [json.loads(line)["event"] for line in wire.read_text().splitlines()] == ["source_identity_rejected"]


def test_gap_delivery_requires_the_single_original_answer_and_real_receipts():
    answer = SimpleNamespace(kind="answer", content="The source failed; result unknown.", references=(),
        provenance={"basis": "settled_whole_answer_submission", "selection_receipts": ["submit"],
                    "attempt_refs": ["failed-read"], "answer_state": "unresolved"})
    versions = [SimpleNamespace(value=answer)]
    assert whole_answer_observed({"submit_research_answer"}, versions, answer.content, None)
    assert not whole_answer_observed({"submit_research_answer"}, versions * 2, answer.content, None)
    assert not whole_answer_observed({"submit_research_answer"}, versions, "changed", None)
    answer.provenance["answer_state"] = "answered"
    assert not whole_answer_observed({"submit_research_answer"}, versions, answer.content, None)


def test_frozen_material_changes_stop_the_existing_matrix(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "source_identity", lambda: {"runtime_id": "unchanged"})
    fixture = tmp_path / "fixtures.json"
    fixture.write_text('{"original":true}')
    identity.freeze_experiment(tmp_path)
    fixture.write_text('{"original":false}')
    with pytest.raises(RuntimeError, match="source changed"):
        identity.verify_experiment(tmp_path)
