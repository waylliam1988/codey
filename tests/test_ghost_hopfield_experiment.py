"""Mathematical, information-boundary, lifecycle and transport contracts for the pilot."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from tests.manual.ghost_memory_routing import hopfield_experiment as experiment
from tests.manual.ghost_memory_routing.hopfield_methods import Index, energy, hopfield_update
from tests.manual.ghost_memory_routing.hopfield_protocol import (
    ARMS,
    CONFIG,
    Gold,
    eligible,
    encode,
    fixture,
    fixture_json,
    render,
    score_action,
)


def test_canonical_update_and_energy_descent() -> None:
    rng = np.random.default_rng(418)
    keys = rng.normal(size=(17, CONFIG["dimension"]))
    keys /= np.linalg.norm(keys, axis=1, keepdims=True)
    state = rng.normal(size=CONFIG["dimension"])
    prior = rng.normal(size=17)
    for _ in range(12):
        logits = CONFIG["beta"] * (keys @ state) + prior
        weights = np.exp(logits - logits.max())
        weights /= weights.sum()
        updated = hopfield_update(keys, state, prior)
        np.testing.assert_allclose(updated, weights @ keys, atol=1e-12)
        assert energy(keys, updated, prior) <= energy(keys, state, prior) + 1e-12
        state = updated


@pytest.mark.parametrize("arm", ARMS)
def test_gold_does_not_affect_selection_or_wire(arm: str) -> None:
    query, gold = fixture()[0]
    other = Gold(("nonexistent",), "ask_user")
    a = experiment.cell(query, gold, arm)
    b = experiment.cell(query, other, arm)
    assert a["selected"] == b["selected"]
    assert a["context"] == b["context"]
    assert experiment.request_body(query, str(a["context"]), "model") == experiment.request_body(
        query, str(b["context"]), "model")


@pytest.mark.parametrize("arm", ARMS)
def test_lifecycle_filters_apply_to_rebuilt_index(arm: str) -> None:
    query = fixture()[-1][0]
    allowed = {memory.id for memory in eligible(query)}
    forbidden = {memory.id for memory in query.memories} - allowed
    assert len(forbidden) == 3  # expired, deleted, wrong user
    for _ in range(2):
        index = Index(query, arm)
        assert {m.id for m in index.memories} <= allowed
        assert not {m.id for m in index.ranked(query)} & forbidden
    prior_query = replace(query, day=29)
    assert any(m.id.endswith("expired") for m in eligible(prior_query))
    assert not any(m.id.endswith("expired") for m in eligible(query))
    # Revocation after a previously allowed build must not survive reconstruction.
    old = Index(prior_query, arm)
    assert any(m.id.endswith("research") for m in old.memories)
    revoked = replace(query, memories=tuple(replace(m, deleted=True) for m in query.memories))
    rebuilt = Index(revoked, arm)
    assert not rebuilt.ranked(revoked)
    assert rebuilt.keys.shape == (0, CONFIG["dimension"])


def test_fixed_state_freezes_prefix_and_rules_adapt_from_actual_feedback() -> None:
    drift = [query for query, _ in fixture() if query.profile == 0 and query.family == "drift"]
    before = Index(drift[0], "fixed_state")
    after = Index(drift[-1], "fixed_state")
    assert before.memories == after.memories
    rule_before = Index(drift[0], "latest_rule").ranked(drift[0])
    rule_after = Index(drift[1], "latest_rule").ranked(drift[1])
    assert rule_before[0].action != rule_after[0].action
    assert rule_after[0].day == 25


def test_fixture_gold_is_separate_and_relevant_entries_exist() -> None:
    cases = fixture()
    assert len(cases) == 44
    assert len({q.id for q, _ in cases}) == 44
    for query, gold in cases:
        assert set(gold.relevant) <= {m.id for m in eligible(query)}
        for memory in eligible(query):
            if memory.id in gold.relevant:
                assert memory.action == gold.action
        assert not any(tag.startswith("observed-value:") for tag in query.tags)
    assert fixture_json() == fixture_json()
    np.testing.assert_array_equal(encode(cases[0][0].tags), encode(cases[0][0].tags))


@pytest.mark.parametrize("arm", ARMS)
def test_exact_render_budget_and_common_vector_inputs(arm: str) -> None:
    for query, _ in fixture():
        index = Index(query, arm)
        context, selected = render(index.ranked(query))
        assert len(context) <= CONFIG["budget_chars"]
        assert len(selected) <= CONFIG["max_memories"]
        for memory, key in zip(index.memories, index.keys, strict=True):
            np.testing.assert_array_equal(key, encode(memory.tags, memory.action))


@pytest.mark.parametrize("raw", ["```json\n{\"action\":\"concise\"}\n```", "{}",
                                     '{"action":"other"}', '{"action":"concise","extra":1}',
                                     '{"action": ["concise"]}', "invalid"])
def test_strict_scorer_rejects_noncontract_replies(raw: str) -> None:
    assert score_action(raw) is None


def test_strict_scorer_accepts_contract() -> None:
    assert score_action('{"action":"concise"}') == "concise"


def test_transport_failure_is_saved_without_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[object] = []

    def fail(*args: object, **kwargs: object) -> object:
        attempts.append(args)
        raise urllib.error.URLError("intentional offline test")

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    with pytest.raises(urllib.error.URLError):
        experiment.generate("http://127.0.0.1:1/v1", b"{}", tmp_path / "request")
    assert len(attempts) == 1
    assert (tmp_path / "request/request.json").read_bytes() == b"{}"
    failure = json.loads((tmp_path / "request/failure.json").read_text())
    assert "intentional offline test" in failure["error"]


def test_freeze_rejects_mutation_and_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(experiment, "identity", lambda: {"frozen": True})
    experiment.freeze(tmp_path)
    experiment.check_freeze(tmp_path)
    with pytest.raises(FileExistsError):
        experiment.freeze(tmp_path)
    monkeypatch.setattr(experiment, "identity", lambda: {"frozen": False})
    with pytest.raises(ValueError, match="changed after freeze"):
        experiment.check_freeze(tmp_path)
