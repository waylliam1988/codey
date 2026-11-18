"""The frozen consumer trial uses exact scoring and complete paired data."""

from __future__ import annotations

from pathlib import Path

import pytest

from codey.agents.request import AgentRequest
from codey.ghost.observation_index import render_retrieved_block, retrieve_relevant_observations
from codey.workspace.context_admission import admit_ghost_context
from tests.manual.ghost_memory_routing import project_experience_continuity as trial
from tests.test_ghost_experience_role_budget import Endpoint
from tests.test_ghost_experience_role_budget import endpoint as endpoint


@pytest.mark.parametrize("index", (0, 1, 8, 9, 18, 19))
def test_frozen_fixture_is_bounded_and_contains_no_future_feedback(index: int) -> None:
    user, assistant, expected = trial.fixture(index)
    assert len(user) == 3000 and expected in assistant
    rows: list[object] = [{"user_text": user, "assistant_text": assistant, "mode": "project", "ts": ""}]
    picked = retrieve_relevant_observations(rows, trial.QUESTION, budget_chars=1800)
    rendered = render_retrieved_block(picked, 1800)
    assert assistant in rendered and len(rendered) <= 1800
    assert "validation remains pending" in rendered


@pytest.mark.parametrize("value,accepted", (
    ("borosql | offline durability", True), (" borosql | offline durability\n", True),
    ("```borosql | offline durability```", False), ("BOROSQL | offline durability", False),
    ("borosql | offline durability. Tested.", False), ("UNKNOWN", False),
))
def test_scorer_does_not_repair_or_relax_model_output(value: str, accepted: bool) -> None:
    assert trial.correct(value, "borosql | offline durability") == accepted


def test_missing_pairs_are_incomplete() -> None:
    with pytest.raises(AssertionError):
        trial.summarize([])


def test_independent_source_obeys_existing_admission(tmp_path: Path) -> None:
    provider = trial.CapturedProvider(tmp_path)
    try:
        request = AgentRequest(provider=provider, project=tmp_path, task=trial.QUESTION,
                               session_id="new", ghost_experiences="bounded history")
        sources = trial.candidate_sources(request)
        scope = sources[0].scope
        admitted = admit_ghost_context("bounded history", sources, request_scope=scope, require_all=True)
        assert admitted.accepted == sources
        blocked = admit_ghost_context("bounded history", sources, request_scope=scope, admit_hints=False)
        assert not blocked.accepted and blocked.rejected[0].reason == "hints_disabled"
    finally:
        provider.close()


def test_service_usage_missing_is_incomplete_without_a_second_request(
    tmp_path: Path, endpoint: Endpoint, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(trial, "ENDPOINT", endpoint.url)
    with pytest.raises(ValueError, match="missing service usage"):
        trial._cell(tmp_path / "trial", 0, False)
    assert len(endpoint.payloads) == 1
    assert (tmp_path / "trial" / "reply.json").is_file()
