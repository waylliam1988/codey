"""Declared outcomes reuse the shared tool receipts and completion gate."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from codey.app.api import run_submit_response
from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.knowledge.model import KnowledgeObject, KnowledgeReference
from codey.knowledge.store import KnowledgeStore
from codey.policies.task_policy import build_task_policy
from tests.test_headless_runner import _RouteProvider
from tests.test_knowledge_repository import example


@pytest.mark.parametrize("invalid", [None, "knowledge_commit", [True], ["KnowledgeCommit"], ["x"] * 17])
def test_api_rejects_invalid_required_tools_before_submitting(invalid):
    submit = Mock()
    status, result = run_submit_response({"task": "save", "required_tools": invalid}, submit)
    assert status == 400 and "required_tools" in result["error"]
    submit.assert_not_called()


def test_declaring_an_outcome_never_grants_permission():
    policy = build_task_policy(SimpleNamespace(project=None, requested_capabilities=(),
        required_tools=("knowledge_commit",), denied_capabilities=("knowledge.write",)), task_kind="hybrid")
    assert "tool_succeeded:knowledge_commit" in policy.required_checks
    assert not policy.allows("knowledge.write")
    submit = Mock(return_value="run")
    assert run_submit_response({"task": "save", "required_tools": ["knowledge_commit", "knowledge_commit"]}, submit)[0] == 200
    assert submit.call_args.kwargs["required_tools"] == ("knowledge_commit",)


@pytest.mark.usefixtures("no_external_advisor_models")
def test_headless_rejects_displayed_commit_then_completes_after_real_read_and_commit(tmp_path):
    root = tmp_path / "knowledge"
    store = KnowledgeStore(root)
    store.repository.commit("premise", example())
    store.close()
    inference = KnowledgeObject("queue-candidate", "inference", "A single queue gate may serialize submissions.",
        "model_inference", references=(KnowledgeReference("claim", 1, "depends_on"),))
    commit = {"commit_id": "queue-candidate", "changes": [{"expected_revision": 0, "value": inference.to_dict()}]}
    provider = _RouteProvider([
        "Candidate example:\n```json\n" + json.dumps({"tool": "knowledge_commit", "args": commit}) + "\n```",
        json.dumps({"tool": "knowledge_read", "args": {"id": "claim", "revision": 1}}),
        json.dumps({"tool": "knowledge_commit", "args": commit}),
        json.dumps({"tool": "done", "args": {"summary": "Saved a candidate, not a measured queue performance result.",
            "knowledge_refs": [{"id": "claim", "revision": 1}, {"id": inference.id, "revision": 1}]}}),
    ])
    events = []
    result = run_headless(HeadlessRequest(project=None, task="Read the previous premise and save a candidate inference.",
        intent="hybrid", provider_id="qwen", max_turns=5, session_id="new", state_home=tmp_path / "state",
        research_store_root=root, requested_capabilities=("knowledge.read", "knowledge.write"),
        required_tools=("knowledge_read", "knowledge_commit")), emit_jsonl=events.append,
        connect_provider=lambda *_args, **_kwargs: provider)
    assert result.exit_code == 0, events[-2:]
    assert [r["tool_name"] for r in events if r["type"] == "tool"] == ["knowledge_read", "knowledge_commit"]
    assert any("Required execution" in prompt and "not done yet" in prompt.lower() for prompt in provider.prompts[1:])
    store = KnowledgeStore(root)
    try:
        saved = store.repository.read(inference.id)
        assert saved.revision == 1 and saved.value == inference
    finally:
        store.close()
