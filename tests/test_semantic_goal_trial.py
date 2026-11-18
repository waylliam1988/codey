"""Causal, source and scoring contracts frozen before the semantic live trial."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from codey.workspace.context_admission import GhostAdmissionError, admit_ghost_context
from tests.manual.ghost_memory_routing import semantic_goal_trial as trial
from tests.test_ghost_experience_role_budget import Endpoint
from tests.test_ghost_experience_role_budget import endpoint as endpoint


def seed() -> trial.Event:
    return trial.Event("seed", "scope", trial.CONTEXT, 0, trial.Priority.OFFLINE, "I confirm offline access first.")


@pytest.mark.parametrize("index", range(trial.STREAMS))
def test_cold_chronological_replay_reverses_and_maintains_goal_for_new_proposals(tmp_path: Path, index: int) -> None:
    path = tmp_path / "events.jsonl"
    trial.append(path, replace(seed(), priority=trial.initial(index)))
    prior_labels: set[str] = set()
    for phase in range(trial.PHASES):
        rows = trial.load(path)
        assert rows == trial.load(path)
        active = frozenset(row.ref for row in rows)
        at = 10 * (phase + 1)
        eligible = trial.applicable(rows, scope="scope", context=trial.CONTEXT, at=at, active=active)
        state = trial.understanding(eligible)
        assert state is not None and state.priority == trial.expected(index, phase)
        labels = trial.proposals(index, phase)
        assert not prior_labels.intersection(labels.values())
        prior_labels.update(labels.values())
        prediction = trial.predict(state, index=index, phase=phase, at=at)
        assert prediction is not None and prediction.expected_label == labels[state.priority]
        history, goal = trial.history_text(eligible), trial.goal_text(state)
        forecast = trial.forecast_text(prediction, scope="scope", at=at, active=active)
        assert f"feedback:{index}:{phase}" not in history + goal + forecast
        text, sources = trial.render_sources(history, goal, forecast, scope="scope")
        assert admit_ghost_context(text, sources, request_scope="scope", require_all=True).accepted == sources
        behavior = trial.Behavior(f"behavior:{phase}", "scope", trial.CONTEXT, at, labels[state.priority])
        feedback = trial.feedback_for(index, phase, behavior)
        outcome = trial.review(prediction, behavior, feedback)
        assert outcome == ("miss" if phase == 0 else "hit" if phase == 1 else "unjudged")
        event = trial.update(behavior, feedback, outcome)
        if phase < 2:
            assert event is not None
            trial.append(path, event)
        else:
            assert event is None
    assert len(trial.load(path)) == 3 and path.stat().st_size <= 65536


@pytest.mark.parametrize("fault", ("absent", "implicit", "ref", "digest", "scope", "context", "before", "quote", "empty_reply"))
def test_invalid_observation_neither_reviews_nor_changes_goal(fault: str) -> None:
    behavior = trial.Behavior("reply", "scope", trial.CONTEXT, 10, "cedar0p0")
    feedback = trial.feedback_for(0, 0, behavior)
    assert feedback is not None
    if fault == "absent":
        feedback = None
    elif fault == "implicit":
        feedback = replace(feedback, confirmed=False)
    elif fault == "ref":
        feedback = replace(feedback, behavior_ref="other")
    elif fault == "digest":
        feedback = replace(feedback, behavior_digest="forged")
    elif fault == "scope":
        feedback = replace(feedback, scope="other")
    elif fault == "context":
        feedback = replace(feedback, context="other")
    elif fault == "before":
        feedback = replace(feedback, at=10)
    elif fault == "quote":
        feedback = replace(feedback, quote="")
    else:
        behavior = replace(behavior, summary="")
    prediction = trial.predict(seed(), index=0, phase=0, at=10)
    assert trial.review(prediction, behavior, feedback) == "unjudged"
    assert trial.update(behavior, feedback, "unjudged") is None


def test_manual_confirmation_does_not_depend_on_prediction_or_forecast_correctness() -> None:
    behavior = trial.Behavior("reply", "scope", trial.CONTEXT, 10, "unexpected but observable text")
    feedback = trial.feedback_for(0, 0, behavior)
    assert trial.review(None, behavior, feedback) == "unjudged"
    revised = trial.update(behavior, feedback, "unjudged")
    assert revised is not None and revised.priority == trial.Priority.SHARED
    prediction = trial.predict(seed(), index=0, phase=0, at=10)
    assert prediction is not None and feedback is not None
    assert trial.review(prediction, behavior, replace(feedback, at=16)) == "unjudged"
    assert trial.review(replace(prediction, scope="other"), behavior, feedback) == "unjudged"
    assert trial.review(replace(prediction, context="other"), behavior, feedback) == "unjudged"
    assert trial.review(prediction, behavior, replace(feedback, assessed_label="")) == "unjudged"


def test_controls_scope_source_retirement_and_horizon() -> None:
    rows, active = (seed(),), frozenset({"seed"})
    assert not trial.applicable(rows, scope="scope", context=trial.CONTEXT, at=10, active=active, retrieve=False)
    assert not trial.applicable(rows, scope="other", context=trial.CONTEXT, at=10, active=active)
    assert not trial.applicable(rows, scope="scope", context="other", at=10, active=active)
    assert not trial.applicable(rows, scope="scope", context=trial.CONTEXT, at=-1, active=active)
    assert not trial.applicable(rows, scope="scope", context=trial.CONTEXT, at=10, active=frozenset())
    prediction = trial.predict(seed(), index=0, phase=0, at=10)
    assert prediction is not None
    assert not trial.forecast_text(prediction, scope="other", at=10, active=active)
    assert not trial.forecast_text(prediction, scope="scope", at=9, active=active)
    assert not trial.forecast_text(prediction, scope="scope", at=15, active=active)
    assert not trial.forecast_text(prediction, scope="scope", at=10, active=frozenset())
    text, sources = trial.render_sources(trial.history_text(rows), trial.goal_text(seed()), "", scope="scope")
    with pytest.raises(GhostAdmissionError):
        admit_ghost_context(text, sources, request_scope="other", require_all=True)
    with pytest.raises(GhostAdmissionError):
        admit_ghost_context(text, sources, request_scope="scope", require_all=True, admit_hints=False)
    assert trial.understanding(()) is None and trial.goal_text(None) == ""


@pytest.mark.parametrize("key,value", (("schema", True), ("at", True), ("at", -1), ("ref", ""),
                                       ("scope", ""), ("context", ""), ("quote", ""), ("priority", "unknown"),
                                       ("extra", "field"), ("behavior_ref", "unbound"),
                                       ("actual_summary", "forged"), ("previous_review", "silence_is_miss")))
def test_invalid_saved_events_fail_at_boundary(key: str, value: object) -> None:
    payload = seed().payload()
    payload[key] = value
    with pytest.raises(ValueError):
        trial.Event.decode(payload)


def test_duplicate_corrupt_and_nonchronological_history_rejected(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    trial.append(path, seed())
    original = path.read_bytes()
    for row in (seed(), replace(seed(), ref="other", at=0)):
        with pytest.raises(ValueError):
            trial.append(path, row)
        assert path.read_bytes() == original
    path.write_bytes(original + original)
    with pytest.raises(ValueError):
        trial.load(path)
    path.write_text('{"bad":true}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        trial.load(path)


def metric_rows(*, retrieval: bool = True, confirmed: bool = True) -> list[dict[str, object]]:
    return [{"stream": index, "phase": phase, "arm": arm, "valid_reply": True,
             "correct": arm == "forecast" or (arm == "confirmed" and confirmed) or (arm == "retrieval" and retrieval),
             "input_tokens": 100, "elapsed_ms": 100.0}
            for index in range(trial.STREAMS) for phase in range(trial.PHASES) for arm in trial.ARMS]


def test_separate_personal_continuity_from_incremental_forecast_benefit() -> None:
    reviews: list[dict[str, object]] = [{"stream": i, "phase": phase, "outcome": "unjudged"}
                                       for i in range(trial.STREAMS) for phase in range(trial.PHASES)]
    metrics = trial.summarize(metric_rows(), reviews, [100] * trial.STREAMS)
    assert metrics["goal_eligible_for_production_boundaries"]
    assert not metrics["forecast_eligible_for_production_boundaries"]
    assert trial.summarize(metric_rows(retrieval=False, confirmed=False), reviews, [100] * trial.STREAMS)["forecast_benefit_gate"]
    with pytest.raises(ValueError, match="missing or duplicate"):
        trial.summarize(metric_rows()[:-1], reviews, [100] * trial.STREAMS)
    with pytest.raises(ValueError, match="missing or duplicate"):
        trial.summarize(metric_rows(), reviews[:-1], [100] * trial.STREAMS)


def test_identical_questions_opposite_personal_truth_and_strict_scoring() -> None:
    for index in range(0, trial.STREAMS, 2):
        for phase in range(trial.PHASES):
            assert trial.question(index, phase) == trial.question(index + 1, phase)
            assert trial.expected(index, phase) != trial.expected(index + 1, phase)
            assert trial.proposals(index, phase) == trial.proposals(index + 1, phase)
            assert "feedback:" not in trial.question(index, phase)
            label = trial.proposals(index, phase)[trial.expected(index, phase)]
            assert label + "!" not in trial.proposals(index, phase).values()
            assert "```" + label + "```" not in trial.proposals(index, phase).values()


def test_kernel_single_physical_send_normal_budget_and_no_missing_usage_retry(
    tmp_path: Path, endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(trial, "ENDPOINT", endpoint.url)
    with pytest.raises(ValueError, match="missing service usage"):
        trial.cell(tmp_path / "cell", index=0, phase=0, arm="confirmed", history="Saved project interactions",
                   goal=trial.goal_text(seed()), forecast="", scope="scope")
    assert len(endpoint.payloads) == 1
    payload = endpoint.payloads[0]
    assert payload["max_tokens"] == 8192
    assert (tmp_path / "cell" / "result.json").is_file()
