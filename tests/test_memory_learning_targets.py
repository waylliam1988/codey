"""Versioned exposure, automatic review and explicit delayed repair attribution."""

from dataclasses import replace
from pathlib import Path

import pytest

from codey.memory.codec import digest
from codey.memory.contracts import MemoryFeedback, MemoryMaintenanceRequest, MemoryPrediction
from tests.test_memory_boundary import NOW, SCOPE, backend, event
from tests.test_memory_lifecycle import EXPIRED, LATER, confirm


def test_multiple_exposures_require_explicit_target_and_preserve_versions(tmp_path: Path) -> None:
    store = backend(tmp_path)
    confirm(store, "pref")
    item = store.records((SCOPE,))[0].item
    version = digest(item.to_payload())
    store.observe(replace(event("sent"), payload={"user_text": "Python", "assistant_text": "Done", "exposures": [
        {"id": "request:one", "item_id": "pref", "item_version": version, "source_ref": "request:one"},
        {"id": "request:two", "item_id": "pref", "item_version": version, "source_ref": "request:two"}]}))
    with pytest.raises(ValueError, match="exposed"):
        store.feedback(MemoryFeedback("ambiguous", "pref", "r", SCOPE, "helpful", "user:feedback", LATER))
    store.feedback(MemoryFeedback("explicit", "pref", "r", SCOPE, "helpful", "user:feedback", LATER, "request:two"))
    first = store.export_state()
    store.rebuild()
    assert store.export_state() == first


def test_due_behavior_review_updates_only_from_matching_exposure(tmp_path: Path) -> None:
    store = backend(tmp_path)
    confirm(store, "pref")
    item = store.records((SCOPE,))[0].item
    store.predict(MemoryPrediction("p", "pref", SCOPE, "Use Python examples", "helpful", ("user:p",), NOW, LATER,
                                   "behavior", "code_fence_language", "python"))
    store.observe(replace(event("observed"), payload={"user_text": "Example", "assistant_text": "Rust", "exposures": [
        {"id": "exposure", "item_id": "pref", "item_version": digest(item.to_payload()), "source_ref": "request:one"}],
        "behaviors": [{"id": "behavior", "run_id": "r", "request_ref": "request:one", "kind": "code_fence_language",
                       "value": "rust", "observation_ref": "answer:one", "workspace_revision": ""}]}))
    store.maintain_batch(MemoryMaintenanceRequest("review:due", EXPIRED))
    predictions = store.export_state()["predictions"]
    assert isinstance(predictions, list) and isinstance(predictions[0], dict)
    assert predictions[0]["status"] == "contradicted"
    assert store.proposals(SCOPE)[0].item.kind == "hypothesis"
    assert store.records((SCOPE,))[0].state == "active"
    before = store.export_state()
    store.rebuild()
    assert store.export_state() == before


def test_repair_credit_requires_real_check_and_exact_revision(tmp_path: Path) -> None:
    store = backend(tmp_path)
    store.observe(replace(event("facts"), kind="task.failed", payload={"outcomes": [
        {"effect_id": "original", "category": "tool_call", "status": "error"},
        {"effect_id": "edit", "category": "tool_call", "status": "ok"},
        {"effect_id": "check", "category": "completion_check", "status": "pass", "value": "pass",
         "workspace_revision": "7:hash", "check_key": "unit_tests"}]}))
    review = replace(event("repair"), kind="memory.repair_reviewed", payload={"repair_id": "repair-one",
        "failure_id": "original", "action_ids": ["edit"], "verification_id": "check", "workspace_revision": "8:other",
        "statement": "Fix the encoding"})
    with pytest.raises(ValueError, match="revision"):
        store.observe(review)
    store.observe(replace(review, payload={**review.payload, "workspace_revision": "7:hash"}))
    assert store.proposals(SCOPE)[0].item.slot == "repair.pattern"
    store.observe(replace(event("regression"), kind="task.failed", occurred_at=LATER, payload={"outcomes": [
        {"effect_id": "later-check", "category": "completion_check", "value": "fail", "status": "fail",
         "workspace_revision": "7:hash", "check_key": "unit_tests"}]}))
    store.observe(replace(event("repair-later"), kind="memory.repair_reviewed", occurred_at=LATER,
        payload={"repair_id": "repair-one", "regression_id": "later-check"}))
    before = store.export_state()
    store.rebuild()
    assert store.export_state() == before


def test_user_prediction_matches_behavior_only_at_its_exposed_request(tmp_path: Path) -> None:
    store = backend(tmp_path)
    from codey.memory.contracts import MemoryDecision

    store.observe(replace(event("user-pref"), kind="memory.proposed", scope="user",
        payload={"kind": "preference", "text": "Use Python examples"}))
    proposal, = store.proposals("user")
    store.decide(MemoryDecision(proposal.proposal_id, proposal.version, True, "user:confirm", NOW))
    item = store.records(("user",))[0].item
    store.predict(MemoryPrediction("user-p", item.item_id, "user", "Python in exposed answer", "helpful", ("user:p",),
        NOW, LATER, "behavior", "code_fence_language", "python"))
    store.observe(replace(event("answer"), payload={"user_text": "Example", "assistant_text": "Python", "exposures": [
        {"id": "e", "item_id": item.item_id, "item_version": digest(item.to_payload()), "source_ref": "request:one"}],
        "behaviors": [{"id": "actual", "run_id": "r", "request_ref": "request:one", "kind": "code_fence_language", "value": "python"},
                      {"id": "unrelated", "run_id": "r", "request_ref": "request:two", "kind": "code_fence_language", "value": "rust"}]}))
    store.maintain_batch(MemoryMaintenanceRequest("user:due", EXPIRED))
    predictions = store.export_state()["predictions"]
    assert isinstance(predictions, list) and isinstance(predictions[0], dict)
    assert predictions[0]["status"] == "supported" and predictions[0]["feedback_id"] == "actual"


def test_management_calibration_does_not_include_another_project(tmp_path: Path) -> None:
    from contextlib import closing

    from codey.app.context import AppContext
    from codey.app.memory_routes import memory_action, memory_summary
    from codey.types.json_value import decode_json_object
    from codey.workspace.context_admission import request_scope

    with closing(AppContext(tmp_path / "state", start_scheduler=False)) as state:
        state.durable.create_conversation("s")
        for project in (str(tmp_path / "a"), str(tmp_path / "b")):
            base: dict[str, object] = {"session_id": "s", "project": project}
            assert memory_action(state, {**base, "action": "propose", "kind": "preference", "text": "Prefer clear examples"})[0] == 200
            proposal, = state.memory.proposals(request_scope(project, "s"))
            assert memory_action(state, {**base, "action": "accept", "proposal_id": proposal.proposal_id, "version": proposal.version})[0] == 200
            assert memory_action(state, {**base, "action": "predict", "item_id": proposal.item.item_id,
                "statement": "Clear examples are helpful", "due_at": "2099-01-01T00:00:00+00:00"})[0] == 200
        status, summary = memory_summary(state, {"session_id": ["s"], "project": [str(tmp_path / "a")]})
        assert status == 200
        assert decode_json_object(summary["calibration"])["unjudged"] == 1
