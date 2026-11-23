"""Feedback requires a delivered exposure; predictions never manufacture evidence."""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from urllib.request import Request

import pytest

from codey.memory.contracts import DeleteScopeRequest, MemoryFeedback, MemoryPrediction, MemoryQuery, MemoryReview
from tests.test_memory_boundary import NOW, SCOPE, backend, event
from tests.test_memory_lifecycle import EXPIRED, LATER, confirm

FEEDBACK_TIME = "2026-11-20T00:00:00+00:00"


def test_feedback_requires_the_actual_exposed_item_and_run(tmp_path: Path) -> None:
    store = backend(tmp_path)
    confirm(store, "pref")
    with pytest.raises(ValueError, match="exposed"):
        store.feedback(MemoryFeedback("missing", "pref", "r", SCOPE, "helpful", "user:feedback", FEEDBACK_TIME))
    store.observe(replace(event("delivered"), payload={"user_text": "Python", "assistant_text": "Done",
        "exposures": [{"item_id": "pref", "source_ref": "request:original"}]}))
    with pytest.raises(ValueError, match="scope"):
        store.feedback(MemoryFeedback("foreign", "pref", "r", "project:other", "helpful", "user:f", FEEDBACK_TIME))
    good = MemoryFeedback("positive", "pref", "r", SCOPE, "helpful", "user:positive", FEEDBACK_TIME)
    store.feedback(good)
    store.feedback(good)
    feedback = store.export_state()["feedback"]
    assert isinstance(feedback, list) and len(feedback) == 1
    with pytest.raises(ValueError, match="already has feedback"):
        store.feedback(replace(good, feedback_id="opposite", rating="correction"))
    assert next(row for row in store.records((SCOPE,)) if row.item.item_id == "pref").state == "active"


def test_explicit_correction_quarantines_until_revised_or_restored(tmp_path: Path) -> None:
    store = backend(tmp_path)
    confirm(store, "pref", slot="examples")
    store.observe(replace(event("delivered"), payload={"user_text": "Python", "assistant_text": "Done",
        "exposures": [{"item_id": "pref", "source_ref": "request:original"}]}))
    store.feedback(MemoryFeedback("correction", "pref", "r", SCOPE, "correction", "user:correction", FEEDBACK_TIME))
    result = store.retrieve(MemoryQuery("Python", (SCOPE,), FEEDBACK_TIME))
    assert "pref" not in {item.item_id for item in result.items}
    assert any(trace.item_id == "pref" and trace.reason == "quarantined" for trace in result.traces)
    before = store.export_state()
    store.rebuild()
    assert store.export_state() == before
    confirm(store, "revised", text="Use concrete Python examples", slot="examples", supersedes=("pref",))
    assert "revised" in {item.item_id for item in store.retrieve(MemoryQuery("Python", (SCOPE,), FEEDBACK_TIME)).items}


@pytest.mark.parametrize("rating,expected", [("helpful", "supported"), ("unhelpful", "contradicted")])
def test_prediction_review_is_computed_from_bound_feedback(tmp_path: Path, rating: str, expected: str) -> None:
    from codey.memory.contracts import FeedbackRating, PredictionOutcome

    ratings: dict[str, FeedbackRating] = {"helpful": "helpful", "unhelpful": "unhelpful"}
    outcomes: dict[str, PredictionOutcome] = {"supported": "supported", "contradicted": "contradicted"}
    store = backend(tmp_path)
    confirm(store, "pref")
    store.predict(MemoryPrediction("prediction", "pref", SCOPE, "This preference will help", "helpful",
                                   ("user:prediction",), NOW, LATER))
    store.observe(replace(event("delivered"), payload={"user_text": "Python", "assistant_text": "Done",
        "exposures": [{"item_id": "pref", "source_ref": "request:original"}]}))
    store.feedback(MemoryFeedback("reaction", "pref", "r", SCOPE, ratings[rating], "user:reaction", FEEDBACK_TIME))
    with pytest.raises(ValueError, match="not due"):
        store.review(MemoryReview("prediction", "reaction", outcomes[expected], "review:early", FEEDBACK_TIME))
    store.review(MemoryReview("prediction", "reaction", outcomes[expected], "review:due", EXPIRED))
    predictions = store.export_state()["predictions"]
    assert isinstance(predictions, list) and len(predictions) == 1
    row = predictions[0]
    assert isinstance(row, dict) and row["status"] == expected
    assert row["feedback_id"] == "reaction"
    before = store.export_state()
    store.rebuild()
    assert store.export_state() == before


def test_no_observation_stays_unjudged_and_cannot_be_called_success(tmp_path: Path) -> None:
    store = backend(tmp_path)
    confirm(store, "pref")
    store.predict(MemoryPrediction("prediction", "pref", SCOPE, "May help", "helpful", ("user:p",), NOW, LATER))
    with pytest.raises(ValueError, match="differs"):
        store.review(MemoryReview("prediction", "", "supported", "review:false", EXPIRED))
    store.review(MemoryReview("prediction", "", "unjudged", "review:honest", EXPIRED))
    predictions = store.export_state()["predictions"]
    assert isinstance(predictions, list) and isinstance(predictions[0], dict)
    assert predictions[0]["status"] == "unjudged"


def test_feedback_outside_prediction_window_is_not_a_reward(tmp_path: Path) -> None:
    store = backend(tmp_path)
    confirm(store, "pref")
    store.predict(MemoryPrediction("prediction", "pref", SCOPE, "May help", "helpful", ("user:p",), NOW, LATER))
    store.observe(replace(event("delivered"), payload={"user_text": "Python", "assistant_text": "Done",
        "exposures": [{"item_id": "pref", "source_ref": "request:original"}]}))
    store.feedback(MemoryFeedback("late", "pref", "r", SCOPE, "helpful", "user:late", EXPIRED))
    with pytest.raises(ValueError, match="outside"):
        store.review(MemoryReview("prediction", "late", "supported", "review:late", EXPIRED))


def test_unknown_outcomes_do_not_become_successes_or_preferences(tmp_path: Path) -> None:
    store = backend(tmp_path)
    store.observe(replace(event("failure"), kind="task.failed", payload={
        "outcomes": [{"effect_id": "effect:unknown", "category": "provider_send", "status": "unknown", "tool_name": ""}],
    }))
    exported = store.export_state()
    assert exported["items"] == [] and exported["proposals"] == []
    outcomes = exported["outcomes"]
    assert isinstance(outcomes, list) and isinstance(outcomes[0], dict) and outcomes[0]["status"] == "unknown"


def test_deleting_item_sources_removes_dependent_learning_projections(tmp_path: Path) -> None:
    store = backend(tmp_path)
    confirm(store, "pref", scope="user")
    store.observe(replace(event("delivered"), provenance=("another:source",), payload={
        "user_text": "Python", "assistant_text": "Done",
        "exposures": [{"item_id": "pref", "source_ref": "request:original"}],
    }))
    store.feedback(MemoryFeedback("reaction", "pref", "r", "user", "helpful", "user:reaction", FEEDBACK_TIME))
    store.predict(MemoryPrediction("prediction", "pref", "user", "May help", "helpful", ("user:p",), NOW, LATER))
    deleted = store.delete_scope(DeleteScopeRequest("session:unrelated", event("pref").provenance, requested_at=LATER))
    assert deleted.deleted == 1
    exported = store.export_state()
    assert exported["feedback"] == exported["predictions"] == exported["exposures"] == []
    assert len(store.records((SCOPE,))) == 1
    store.rebuild()
    assert store.export_state() == exported


@pytest.mark.parametrize("contextual", [False, True])
def test_normal_headless_receipt_drives_explicit_user_feedback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                        contextual: bool) -> None:
    from codey.app.context import AppContext
    from codey.app.headless_runner import HeadlessRequest, run_headless
    from codey.app.memory_routes import memory_action
    from codey.memory.context import retrieve_context
    from codey.providers import api_transport
    from codey.providers.api_provider import ApiProvider
    from codey.providers.base import ChatProvider
    from codey.types.json_value import require_json_string
    from codey.workspace.context_admission import request_scope

    sent: list[bytes] = []

    class Response(BytesIO):
        headers = {"Content-Type": "application/json"}

    def send(request: Request, timeout: float) -> Response:
        assert isinstance(request.data, bytes)
        sent.append(request.data)
        from codey.types.json_value import JsonObject

        done: JsonObject = {"tool": "done", "args": {"summary": "A concise Python example."}}
        reply: JsonObject = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant",
            "content": json.dumps(done)}}]}
        return Response(json.dumps(reply).encode())

    monkeypatch.setattr(api_transport, "open_request", send)
    project = tmp_path / "project"
    project.mkdir()
    state_home = tmp_path / "state"
    with closing(AppContext(state_home, start_scheduler=False)) as state:
        state.durable.create_conversation("s")
        assert memory_action(state, {"action": "propose", "scope_kind": "user", "session_id": "s",
                                     "kind": "preference", "text": "Prefer concise Python examples",
                                     "slot": "code.language", "value": "python"})[0] == 200
        proposal, = state.memory.proposals("user")
        assert memory_action(state, {"action": "accept", "scope_kind": "user", "session_id": "s",
                                     "proposal_id": proposal.proposal_id, "version": proposal.version})[0] == 200

    def connect(provider_id: str, *, port: int) -> ChatProvider:
        return ApiProvider("http://fixture.invalid/v1", "fixture", native_tools=False, stream=False, thinking_enabled=False)

    events: list[dict[str, object]] = []
    result = run_headless(HeadlessRequest(project=project if contextual else None, task="Hello", intent="chat", session_id="s",
        provider_id="qwen", max_turns=2, state_home=state_home), emit_jsonl=events.append, connect_provider=connect)
    assert result.exit_code == 0, events[-1]
    assert len(sent) == 1 and b"Prefer concise Python examples" in sent[0]
    with closing(AppContext(state_home, start_scheduler=False)) as state:
        from codey.app.memory_observation import routing_for_run
        from codey.app.memory_setup import observe_terminal

        assert state.managed_outputs is not None
        routed = routing_for_run(state.durable, state.managed_outputs, result.run_id)
        assert routed and isinstance(routed[0], dict)
        traces = routed[0]["trace"]
        assert isinstance(traces, list) and any(isinstance(row, dict) and row["reason"] == "selected" for row in traces)
        exposed = state.memory.export_state()["exposures"]
        assert isinstance(exposed, list) and len(exposed) == 1
        assert isinstance(exposed[0], dict) and exposed[0]["item_id"] == proposal.item.item_id
        original = state.memory.rebuild()
        observe_terminal(state.memory, state.durable, result.run_id, state.managed_outputs)
        assert state.memory.rebuild() == original
        command = {"action": "feedback", "scope_kind": "user", "session_id": "s", "request_id": "my-correction",
                   "item_id": proposal.item.item_id, "run_id": result.run_id, "rating": "correction"}
        if contextual:
            from codey.types.json_value import JsonObject

            correction = "In this project, use Go for code examples."
            feedback: JsonObject = {**command, "correction_text": correction,
                "exposure_id": require_json_string(exposed[0]["id"], "exposure_id")}
            assert memory_action(state, {**feedback, "exposure_id": "different", "request_id": "bad"})[0] == 400
            status, settled = memory_action(state, feedback)
            assert status == 200 and memory_action(state, feedback) == (status, settled)
            scope = request_scope(str(project), "s")
            proposal_row, = state.memory.proposals(scope)
            assert proposal_row.item.text == correction and proposal_row.item.value == "go"
            assert proposal_row.relations[0].relation == "context_exception"
            assert exposed[0]["source_ref"] in proposal_row.item.source_refs
            assert state.memory.records(("user",))[0].state == "active"
            assert {item.value for item in retrieve_context(state.memory, task="Show an example", project=str(project),
                    session_id="fresh").items if item.slot} == {"python"}
            assert memory_action(state, {"action": "accept", "project": str(project), "session_id": "s",
                "proposal_id": proposal_row.proposal_id, "version": proposal_row.version})[0] == 200
            assert {item.value for item in retrieve_context(state.memory, task="Show an example", project=str(project),
                    session_id="fresh").items if item.slot} == {"go"}
            assert {item.value for item in retrieve_context(state.memory, task="Show an example", project="",
                    session_id="fresh").items if item.slot} == {"python"}
        else:
            assert memory_action(state, command)[0] == 200
            assert state.memory.records(("user",))[0].state == "quarantined"
        assert memory_action(state, {"action": "control", "scope_kind": "user", "session_id": "s",
            "control": "retrieve_projections", "enabled": False})[0] == 200
        assert routing_for_run(state.durable, state.managed_outputs, result.run_id) == routed
        assert state.durable.task(result.run_id).phase == "completed"
        before = state.memory.export_state()
        state.memory.rebuild()
        assert state.memory.export_state() == before
