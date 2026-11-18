"""Deterministic contracts for the conditional proposal-risk experiment."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from codey.workspace.context_admission import GhostAdmissionError, admit_ghost_context
from tests.manual.ghost_memory_routing import proposal_risk_trial as trial
from tests.manual.ghost_memory_routing import semantic_goal_trial
from tests.test_ghost_experience_role_budget import Endpoint
from tests.test_ghost_experience_role_budget import endpoint as endpoint


@pytest.mark.parametrize("index", range(trial.STREAMS))
def test_cold_replay_causal_feedback_sources_and_budgets(tmp_path: Path, index: int) -> None:
    path = tmp_path / "events.jsonl"
    initial = trial.seed_events(index, "scope")
    for row in initial:
        trial.append(path, row)
    for phase in range(trial.PHASES):
        rows = trial.load(path)
        assert rows == trial.load(path)
        active = frozenset(row.ref for row in rows)
        at = 1000 * (phase + 1)
        matching = trial.eligible(rows, scope="scope", at=at, active=active)
        predictions = trial.predict(matching, index=index, phase=phase, scope="scope", at=at)
        assert len(predictions) == 2 and all(trial.valid_prediction(row) for row in predictions)
        assert all(set(row.basis).issubset(active) for row in predictions)
        hint = trial.projection(predictions, index=index, phase=phase, scope="scope", at=at, active=active)
        assert hint and len(hint) <= trial.FORECAST_BUDGET
        counts = trial.counts_text(matching)
        assert len(counts) <= trial.COUNTS_BUDGET
        text, sources = trial.render_sources(trial.history_text(matching), counts, hint, scope="scope")
        assert f"feedback:{index}:{phase}" not in text
        assert len(sources) == 3
        assert admit_ghost_context(text, sources, request_scope="scope", require_all=True).accepted == sources
        behavior = trial.Behavior(f"actual:{phase}", "scope", trial.context_for(phase), at, trial.expected(rows, index, phase))
        observations = trial.feedback(index, phase, behavior)
        assert len(observations) == (2 if phase < 2 else 0)
        for prediction in predictions:
            event = next((row for row in observations if row.strategy == prediction.strategy), None)
            outcome, brier = trial.review(prediction, behavior, event, active=active)
            assert (outcome == "unjudged") == (phase == 2)
            assert (brier is None) == (phase == 2)
            assert prediction.p == trial.probability(matching, prediction.context, prediction.strategy)
        for observation in observations:
            trial.append(path, observation)
    assert len(trial.load(path)) == len(initial) + 4
    assert path.stat().st_size <= trial.STATE_LIMIT


def test_identical_paired_questions_personal_history_changes_actions() -> None:
    different = 0
    asks = 0
    for index in range(0, trial.STREAMS, 2):
        for phase in range(trial.PHASES):
            assert trial.question(index, phase) == trial.question(index + 1, phase)
            left = trial.expected(trial.seed_events(index, "scope"), index, phase)
            right = trial.expected(trial.seed_events(index + 1, "scope"), index + 1, phase)
            different += left != right
            asks += left == trial.ASK or right == trial.ASK
            assert trial.expected((), index, phase) == trial.ASK
    assert different == 30
    assert asks > 0


@pytest.mark.parametrize("fault", ("absent", "implicit", "ref", "digest", "scope", "context", "strategy", "before",
                                   "late", "quote", "summary", "reply_time", "source", "probability"))
def test_invalid_or_missing_assessment_is_unjudged(fault: str) -> None:
    rows = trial.seed_events(0, "scope")
    active = frozenset(row.ref for row in rows)
    prediction = trial.predict(rows, index=0, phase=0, scope="scope", at=1000)[0]
    behavior = trial.Behavior("reply", "scope", trial.Context.FIELD, 1000, "cedar0p0")
    event: trial.Event | None = trial.feedback(0, 0, behavior)[0]
    assert event is not None
    if fault == "absent":
        event = None
    elif fault == "implicit":
        event = replace(event, quote="Thanks")
    elif fault == "ref":
        event = replace(event, reply_ref="other")
    elif fault == "digest":
        event = replace(event, reply_digest="forged")
    elif fault == "scope":
        event = replace(event, scope="other")
    elif fault == "context":
        event = replace(event, context=trial.Context.TEAM)
    elif fault == "strategy":
        event = replace(event, strategy=trial.Strategy.SHARED)
    elif fault == "before":
        event = replace(event, at=1000)
    elif fault == "late":
        event = replace(event, at=1006)
    elif fault == "quote":
        event = replace(event, quote="")
    elif fault == "summary":
        behavior = replace(behavior, summary="")
    elif fault == "reply_time":
        event = replace(event, reply_at=999)
    elif fault == "source":
        active = frozenset()
    else:
        prediction = replace(prediction, denominator=0)
    assert trial.review(prediction, behavior, event, active=active) == ("unjudged", None)


@pytest.mark.parametrize("fault", ("scope", "before", "expired", "source", "order", "option", "strategy",
                                   "duplicate_basis", "empty_basis", "fraction", "missing"))
def test_invalid_forecast_never_projects(fault: str) -> None:
    rows = trial.seed_events(0, "scope")
    active = frozenset(row.ref for row in rows)
    predictions = trial.predict(rows, index=0, phase=0, scope="scope", at=1000)
    at, scope = 1000, "scope"
    if fault == "scope":
        scope = "other"
    elif fault == "before":
        at = 999
    elif fault == "expired":
        at = 1005
    elif fault == "source":
        active = frozenset()
    elif fault == "order":
        predictions = tuple(reversed(predictions))
    elif fault == "option":
        predictions = (replace(predictions[0], option="forged"), predictions[1])
    elif fault == "strategy":
        predictions = (replace(predictions[0], strategy=trial.Strategy.SHARED), predictions[1])
    elif fault == "duplicate_basis":
        predictions = (replace(predictions[0], basis=("seed:0", "seed:0")), predictions[1])
    elif fault == "empty_basis":
        predictions = (replace(predictions[0], basis=()), predictions[1])
    elif fault == "fraction":
        predictions = (replace(predictions[0], numerator=100), predictions[1])
    else:
        predictions = predictions[:1]
    assert not trial.projection(predictions, index=0, phase=0, scope=scope, at=at, active=active)


@pytest.mark.parametrize("key,value", (("schema", True), ("accepted", 1), ("accepted", "true"), ("at", True),
                                       ("at", -1), ("reply_at", True), ("ref", ""), ("scope", ""),
                                       ("strategy", "unknown"), ("context", "unknown"), ("quote", "Thanks"),
                                       ("actual_summary", "forged"), ("reply_ref", ""), ("extra", "field")))
def test_strict_event_decode(key: str, value: object) -> None:
    payload = trial.seed_events(0, "scope")[0].payload()
    payload[key] = value
    with pytest.raises(ValueError):
        trial.Event.decode(payload)


def test_duplicate_corruption_chronology_and_controls(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    seed = trial.seed_events(0, "scope")[0]
    trial.append(path, seed)
    original = path.read_bytes()
    for row in (seed, replace(seed, ref="other")):
        with pytest.raises(ValueError):
            trial.append(path, row)
        assert path.read_bytes() == original
    path.write_bytes(original + original)
    with pytest.raises(ValueError):
        trial.load(path)
    path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        trial.load(path)
    rows, active = (seed,), frozenset({seed.ref})
    assert not trial.eligible(rows, scope="scope", at=1000, active=active, retrieve=False)
    assert not trial.eligible(rows, scope="other", at=1000, active=active)
    assert not trial.eligible(rows, scope="scope", at=0, active=active)
    assert not trial.eligible(rows, scope="scope", at=1000, active=frozenset())
    text, sources = trial.render_sources(trial.history_text(rows), trial.counts_text(rows), "", scope="scope")
    with pytest.raises(GhostAdmissionError):
        admit_ghost_context(text, sources, request_scope="other", require_all=True)
    with pytest.raises(GhostAdmissionError):
        admit_ghost_context(text, sources, request_scope="scope", require_all=True, admit_hints=False)


def metrics(*, tied: bool = False) -> list[dict[str, object]]:
    return [{"stream": index, "phase": phase, "arm": arm, "valid_reply": True,
             "correct": tied or arm == "forecast", "input_tokens": 100, "elapsed_ms": 100.0}
            for index in range(trial.STREAMS) for phase in range(trial.PHASES) for arm in trial.ARMS]


def reviews() -> list[dict[str, object]]:
    return [{"stream": index, "phase": phase, "strategy": strategy.value,
             "outcome": "hit" if phase < 2 else "unjudged", "brier": .1 if phase < 2 else None}
            for index in range(trial.STREAMS) for phase in range(trial.PHASES) for strategy in trial.Strategy]


def test_fixed_gates_reject_ties_missing_cells_costs_and_prediction_failure() -> None:
    assert trial.summarize(metrics(), reviews(), [100] * trial.STREAMS)["eligible_for_production_boundaries"]
    assert not trial.summarize(metrics(tied=True), reviews(), [100] * trial.STREAMS)["benefit_gate"]
    assert not trial.summarize(metrics(), reviews(), [trial.STATE_LIMIT + 1] * trial.STREAMS)["cost_gate"]
    bad = reviews()
    bad[0]["brier"] = None
    assert not trial.summarize(metrics(), bad, [100] * trial.STREAMS)["prediction_quality_gate"]
    for cells, judged in ((metrics()[:-1], reviews()), (metrics(), reviews()[:-1])):
        with pytest.raises(ValueError, match="missing or duplicate"):
            trial.summarize(cells, judged, [100] * trial.STREAMS)


def test_single_physical_send_normal_budget_and_no_retry_on_missing_usage(
    tmp_path: Path, endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(semantic_goal_trial, "ENDPOINT", endpoint.url)
    with pytest.raises(ValueError, match="missing service usage"):
        trial.cell(tmp_path / "cell", index=0, phase=0, arm="counts", rows=trial.seed_events(0, "scope"),
                   forecast="", scope="scope")
    assert len(endpoint.payloads) == 1
    assert endpoint.payloads[0]["max_tokens"] == 8192
    assert (tmp_path / "cell" / "result.json").is_file()
