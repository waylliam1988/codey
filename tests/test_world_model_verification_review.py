"""Boundary and consumer checks for the isolated World Model candidate."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, tzinfo
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.ghost.payload_types import object_fields
from codey.workspace.context_admission import GhostAdmissionError
from tests.manual.ghost_memory_routing.world_model_verification_review import (
    AS_OF,
    BUDGET,
    CASES,
    EXPIRY,
    SECOND,
    THIRD,
    Case,
    Review,
    VerificationEvent,
    action,
    digest,
    fixture,
    load_event,
    parse_usage,
    predict,
    produce_verification,
    projection,
    render_request,
    review,
)


def _case_id(value: object) -> str:
    if not isinstance(value, Case):
        raise ValueError("not a World Model case")
    return value.name


@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_real_verification_proof_prediction_review_context_consumer(tmp_path: Path, case: Case) -> None:
    base, hint, result = fixture(tmp_path, case)
    expected = {"repeated_environment": "hit", "recovered_environment": "miss", "product_failure": "hit",
                "missing_verification": "unjudged", "changed_workspace": "unjudged"}
    assert result.outcome == expected[case.name]
    assert bool(hint) == (result.outcome != "unjudged")
    audit: list[dict[str, object]] = []
    prompt = render_request(base, hint, scope="user", as_of=THIRD, audit=audit)
    assert base in prompt and len(hint) < BUDGET
    assert "evidence_refs" not in hint
    assert len(audit) == 1
    assert hint not in json.dumps(audit) if hint else "World Model" not in json.dumps(audit)
    assert render_request(base, "", scope="user", as_of=THIRD) == base
    assert digest(prompt) == digest(render_request(base, hint, scope="user", as_of=THIRD))


def _events(home: Path, scope: str = "user") -> tuple[VerificationEvent, VerificationEvent]:
    prior, prior_proof = produce_verification(home / "prior", mode="environment", scope=scope,
                                              observed_at=AS_OF, fingerprint="fixture-workspace-v1")
    current, current_proof = produce_verification(home / "current", mode="pass", scope=scope,
                                                  observed_at=SECOND, fingerprint="fixture-workspace-v1")
    assert not prior_proof.satisfied and current_proof.satisfied
    assert prior.source_ref != current.source_ref
    assert prior_proof.evidence_refs == current_proof.evidence_refs == ()
    return prior, current


@pytest.mark.parametrize("scope", ("user", "project:a", "session:a"))
def test_review_requires_later_verification_and_exact_identity(tmp_path: Path, scope: str) -> None:
    prior, current = _events(tmp_path, scope)
    prediction = predict(prior)
    assert review(prediction, current, as_of=THIRD).outcome == "miss"
    for changed in (
        None, prior, replace(current, scope="other"), replace(current, command_digest=digest("other")),
        replace(current, workspace_fingerprint="different"), replace(current, occurred_at=EXPIRY),
    ):
        result = review(prediction, changed, as_of=THIRD)
        assert result.outcome == "unjudged" and not result.review_ref and result.observed is None
    assert review(prediction, current, as_of=AS_OF).outcome == "unjudged"
    assert review(prediction, current, as_of=EXPIRY).outcome == "unjudged"


@pytest.mark.parametrize("scope", ("user", "project:a", "session:a"))
def test_projection_scope_expiry_revocation_and_removed_sources_cannot_reappear(tmp_path: Path, scope: str) -> None:
    prior, current = _events(tmp_path, scope)
    prediction = predict(prior)
    result = review(prediction, current, as_of=THIRD)
    refs = frozenset((prior.source_ref, current.source_ref))
    assert projection(prediction, result, scope=scope, as_of=THIRD, active_refs=refs)
    assert not projection(prediction, result, scope="other", as_of=THIRD, active_refs=refs)
    assert not projection(prediction, result, scope=scope, as_of=EXPIRY, active_refs=refs)
    assert not projection(prediction, result, scope=scope, as_of=THIRD, active_refs=refs, retrieve=False)
    for removed in (frozenset(), frozenset((prior.source_ref,)), frozenset((current.source_ref,))):
        assert not projection(prediction, result, scope=scope, as_of=THIRD, active_refs=removed)
        # Reconstructing the pure candidate from the original fixture must
        # still consult current source liveness. This is not durable deletion proof.
        restored = load_event(tmp_path / "prior" / "verification.json")
        assert restored is not None
        assert not projection(predict(restored), result, scope=scope, as_of=THIRD, active_refs=removed)
    assert not projection(prediction, Review("unjudged", "no_proof"), scope=scope, as_of=THIRD, active_refs=refs)


def test_committed_input_reopen_and_corruption_fail_closed(tmp_path: Path) -> None:
    prior, _ = _events(tmp_path)
    path = tmp_path / "prior" / "verification.json"
    encoded = path.read_text(encoding="utf-8")
    assert load_event(path) == prior
    assert predict(load_event(path) or prior) == predict(prior)
    for replacement in ("null", "[]", "{}", "not-json"):
        path.write_text(replacement, encoding="utf-8")
        assert load_event(path) is None
    for key, value in (("occurred_at", "bad-time"), ("source_ref", "fake"), ("category", "verified"),
                       ("proof_status", "complete"), ("scope", False)):
        raw: object = json.loads(encoded)
        assert object_fields(raw)
        event = raw["event"]
        assert object_fields(event)
        event[key] = value
        path.write_text(json.dumps(raw), encoding="utf-8")
        assert load_event(path) is None
    path.unlink()
    assert load_event(path) is None
    path.write_text(encoded, encoding="utf-8")
    assert load_event(path) == prior


def test_independent_context_source_admission_disable_expiry_and_budget(tmp_path: Path) -> None:
    base, hint, _ = fixture(tmp_path, CASES[0])
    with pytest.raises(GhostAdmissionError, match="hints_disabled"):
        render_request(base, hint, scope="user", as_of=THIRD, admit=False)
    with pytest.raises(GhostAdmissionError, match="expired"):
        render_request(base, hint, scope="user", as_of=EXPIRY)
    with pytest.raises(ValueError, match="truncated"):
        render_request(base, hint * 10, scope="user", as_of=THIRD)
    class ForbiddenClock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> ForbiddenClock:
            raise AssertionError("implicit wall clock")

    with patch("codey.workspace.context_admission.datetime", ForbiddenClock), patch(
        "codey.ghost.graph_primitives.datetime", ForbiddenClock,
    ):
        prior, current = _events(tmp_path / "clock")
        result = review(predict(prior), current, as_of=THIRD)
        assert result.outcome == "miss"
        assert render_request(base, hint, scope="user", as_of=THIRD).endswith(hint)


@pytest.mark.parametrize(("reply", "expected"), (
    ('{"action":"verify"}', "verify"), ('{"action":"inspect_source"}', "inspect_source"),
    ('{"action":"report_verified"}', "report_verified"), ('{"action":"inspect_environment"}', "inspect_environment"),
    ('{"action":"done"}', "invalid_action"), ('{"action":true}', "invalid_action"),
    ('{"action":"verify","extra":1}', "invalid_structure"), ('[]', "invalid_structure"),
    ('```json\n{"action":"verify"}\n```', "invalid_json"), ('', "invalid_json"),
))
def test_frozen_action_scorer_does_not_guess_or_accept_extra_keys(reply: str, expected: str) -> None:
    assert action(reply) == expected


def test_provider_usage_is_validated_at_boundary() -> None:
    valid = parse_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 3}})
    assert valid is not None and valid.input_tokens == 10 and valid.output_tokens == 3
    invalid_rows: tuple[object, ...] = (
        None, dict[str, object](), {"prompt_tokens": False, "completion_tokens": 3},
        {"prompt_tokens": -1, "completion_tokens": 3}, {"prompt_tokens": 10},
    )
    for invalid in invalid_rows:
        assert parse_usage({"usage": invalid}) is None
