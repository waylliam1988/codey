"""Freeze the causal feedback contract before any new live generations."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from codey.workspace.context_admission import GhostAdmissionError, admit_ghost_context
from tests.manual.ghost_memory_routing import interaction_feedback_trial as trial
from tests.manual.ghost_memory_routing import project_experience_continuity as continuity
from tests.test_ghost_experience_role_budget import Endpoint
from tests.test_ghost_experience_role_budget import endpoint as endpoint


def seed() -> trial.Experience:
    return trial.Experience("seed", "scope", trial.PLAN, 0, None, trial.Opening.COMPARE,
                            "For architecture_plan discussions, compare options first.")


@pytest.mark.parametrize("index", range(trial.STREAMS))
def test_chronological_cold_replay_changes_maintains_and_expires_understanding(tmp_path: Path, index: int) -> None:
    path = tmp_path / "experiences.jsonl"
    initial = replace(seed(), desired=trial.initial(index), quote=f"I prefer {trial.initial(index).value}.")
    trial.append(path, initial)
    for phase in range(trial.PHASES):
        rows = trial.load(path)
        assert rows == trial.load(path)
        active = frozenset(row.ref for row in rows)
        at = 10 * (phase + 1)
        prediction = trial.predict(rows, scope="scope", context=trial.context_for(index, phase), at=at, active_refs=active)
        if phase < 4:
            assert prediction is not None and prediction.expected == trial.expected(index, phase)
        elif trial.context_for(index, phase) != trial.PLAN:
            assert prediction is None
        else:
            assert prediction is not None and prediction.expected != trial.expected(index, phase)
        history = trial.history_text(rows, scope="scope", at=at, active_refs=active)
        assert len(history) <= trial.HISTORY_BUDGET
        assert f"feedback:{index}:{phase}" not in history
        projected = trial.projection(prediction, active_refs=active, at=at, scope="scope")
        assert len(projected) <= trial.FORECAST_BUDGET
        rendered, sources = trial.render_sources(history, projected, scope="scope")
        admitted = admit_ghost_context(rendered, sources, request_scope="scope", require_all=True)
        assert admitted.accepted == sources
        summary = trial.replies(index)[trial.expected(index, phase)]
        behavior = trial.Behavior(f"behavior:{index}:{phase}", "scope", trial.context_for(index, phase), at,
                                  summary, trial.opening_of(summary, index))
        feedback = trial.feedback_for(index, phase, behavior) if phase < 4 else None
        review = trial.review(prediction, behavior, feedback)
        assert review.outcome == ("miss" if phase < 2 else "hit" if phase < 4 else "unjudged")
        revised = trial.update(prediction, behavior, feedback)
        if phase < 4:
            assert revised is not None
            trial.append(path, revised)
        else:
            assert revised is None
    assert len(trial.load(path)) == 5
    assert path.stat().st_size <= 65536


@pytest.mark.parametrize("fault", (
    "missing", "implicit", "behavior_ref", "digest", "scope", "context", "before", "late", "invalid_behavior", "expired",
))
def test_unattributable_or_incomplete_feedback_never_updates_state(fault: str) -> None:
    prediction = trial.predict((seed(),), scope="scope", context=trial.PLAN, at=10, active_refs=frozenset({"seed"}))
    behavior = trial.Behavior("behavior", "scope", trial.PLAN, 10, trial.replies(0)[trial.Opening.COMPARE], trial.Opening.COMPARE)
    feedback: trial.Feedback | None = trial.feedback_for(0, 0, behavior)
    assert feedback is not None
    if fault == "missing":
        feedback = None
    elif fault == "implicit":
        feedback = replace(feedback, explicit=False)
    elif fault == "behavior_ref":
        feedback = replace(feedback, behavior_ref="another")
    elif fault == "digest":
        feedback = replace(feedback, behavior_digest="forged")
    elif fault == "scope":
        feedback = replace(feedback, scope="other")
    elif fault == "context":
        feedback = replace(feedback, context="incident_status")
    elif fault == "before":
        feedback = replace(feedback, at=10)
    elif fault == "late":
        feedback = replace(feedback, at=16)
    elif fault == "invalid_behavior":
        behavior = replace(behavior, opening=None)
    else:
        feedback = replace(feedback, until=feedback.at)
    assert trial.review(prediction, behavior, feedback).outcome == "unjudged"
    assert trial.update(prediction, behavior, feedback) is None


def test_source_controls_scope_future_time_deletion_and_projection_horizon() -> None:
    row = seed()
    refs = frozenset({row.ref})
    prediction = trial.predict((row,), scope="scope", context=trial.PLAN, at=10, active_refs=refs)
    assert prediction is not None
    assert trial.predict((row,), scope="scope", context=trial.PLAN, at=10, active_refs=refs, retrieve=False) is None
    assert trial.predict((row,), scope="other", context=trial.PLAN, at=10, active_refs=refs) is None
    assert trial.predict((row,), scope="scope", context="incident_status", at=10, active_refs=refs) is None
    assert trial.predict((row,), scope="scope", context=trial.PLAN, at=10, active_refs=frozenset()) is None
    assert trial.predict((replace(row, at=11),), scope="scope", context=trial.PLAN, at=10, active_refs=refs) is None
    assert not trial.projection(prediction, active_refs=refs, at=prediction.due, scope="scope")
    assert not trial.projection(prediction, active_refs=frozenset(), at=10, scope="scope")
    assert not trial.projection(prediction, active_refs=refs, at=10, scope="other")
    hint = trial.projection(prediction, active_refs=refs, at=10, scope="scope")
    text, sources = trial.render_sources("", hint, scope="scope")
    assert len(sources) == 1 and sources[0].key == "world_model_interaction_review"
    with pytest.raises(GhostAdmissionError, match="hints_disabled"):
        admit_ghost_context(text, sources, request_scope="scope", require_all=True, admit_hints=False)
    denied = admit_ghost_context(text, sources, request_scope="scope", admit_hints=False)
    assert not denied.accepted and denied.rejected[0].reason == "hints_disabled"


@pytest.mark.parametrize("key,value", (
    ("at", True), ("until", 0), ("desired", "unknown"), ("scope", ""), ("ref", ""),
    ("schema_version", True), ("previous_review", "silence_is_negative"), ("extra", "field"),
    ("behavior_ref", "unbound"), ("actual_summary", "forged"),
))
def test_saved_experience_rejects_invalid_data(key: str, value: object) -> None:
    payload = seed().to_payload()
    payload[key] = value
    with pytest.raises(ValueError):
        trial.Experience.from_payload(payload)


def test_duplicate_append_and_corrupt_replay_do_not_silently_reconstruct(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    trial.append(path, seed())
    original = path.read_bytes()
    with pytest.raises(ValueError, match="duplicate"):
        trial.append(path, seed())
    assert path.read_bytes() == original
    path.write_text('{"broken":true}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        trial.load(path)


def metrics(*, retrieval_correct: bool = True) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(trial.STREAMS):
        for phase in range(trial.PHASES):
            for arm in trial.ARMS:
                rows.append({"stream": index, "phase": phase, "arm": arm, "primary": phase < 4,
                             "correct": arm == "forecast" or (arm == "retrieval" and retrieval_correct),
                             "input_tokens": 100, "elapsed_ms": 100.0})
    return rows


def test_equal_same_history_baseline_prevents_promotion_and_missing_pairs_are_incomplete() -> None:
    rows = metrics()
    reviews: list[dict[str, object]] = [{"stream": index, "phase": phase, "outcome": "unjudged"}
                                       for index in range(trial.STREAMS) for phase in range(trial.PHASES)]
    assert not trial.summarize(rows, reviews, [100] * trial.STREAMS)["benefit_gate"]
    assert trial.summarize(metrics(retrieval_correct=False), reviews, [100] * trial.STREAMS)["benefit_gate"]
    with pytest.raises(ValueError, match="missing or duplicate"):
        trial.summarize(rows[:-1], reviews, [100] * trial.STREAMS)
    rows[-1] = dict(rows[0])
    with pytest.raises(ValueError, match="missing or duplicate"):
        trial.summarize(rows, reviews, [100] * trial.STREAMS)


def test_exact_reply_scorer_and_no_history_truth_leak() -> None:
    for index in range(trial.STREAMS):
        for phase in range(trial.PHASES):
            reply = trial.replies(index)[trial.expected(index, phase)]
            assert trial.opening_of(reply, index) == trial.expected(index, phase)
            assert trial.opening_of(reply + "!", index) is None
            assert trial.opening_of("```" + reply + "```", index) is None
            task = trial.question(index, phase)
            assert "feedback:" not in task and "manual:" not in task
            if phase < 4 and index % 2 == 0:
                assert task == trial.question(index + 1, phase)
                assert trial.expected(index, phase) != trial.expected(index + 1, phase)


def test_actual_kernel_has_one_physical_send_and_no_usage_retry(
    tmp_path: Path, endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(continuity, "ENDPOINT", endpoint.url)
    prediction = trial.predict((seed(),), scope="scope", context=trial.PLAN, at=10, active_refs=frozenset({"seed"}))
    hint = trial.projection(prediction, active_refs=frozenset({"seed"}), at=10, scope="scope")
    with pytest.raises(ValueError, match="missing service usage"):
        trial.cell(tmp_path / "cell", index=0, phase=0, arm="forecast", history="saved history", forecast=hint, scope="scope")
    assert len(endpoint.payloads) == 1
    assert (tmp_path / "cell" / "reply.json").is_file()
