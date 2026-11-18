"""Boundary and mathematical checks; no local generation in pytest."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from tests.manual.ghost_memory_routing import hopfield_v2_live as live
from tests.manual.ghost_memory_routing import hopfield_v2_study as study
from tests.manual.ghost_memory_routing.hopfield_v2_data import (
    ARMS,
    BETAS,
    BUDGET,
    DIMENSION,
    Encoder,
    Gold,
    Parameters,
    Query,
    dataset,
    eligible,
    observable_candidates,
    render,
    stream,
)
from tests.manual.ghost_memory_routing.hopfield_v2_methods import (
    GhostCache,
    Prepared,
    deletion_probe,
    select,
    state_update,
)


def test_independent_streams_and_disjoint_splits() -> None:
    dev, heldout = dataset("dev"), dataset("test")
    assert len(dev) == 24 * 14
    assert len(heldout) == 96 * 14
    assert not {q.stream for q, _ in dev} & {q.stream for q, _ in heldout}
    assert not {event.id for q, _ in dev for event in q.events} & {
        event.id for q, _ in heldout for event in q.events
    }
    histories = {tuple((event.facets, event.day, event.action) for event in stream("test", n)[0][0].events)
                 for n in range(96)}
    assert len(histories) == 96


def test_gold_validity_and_query_answer_separation() -> None:
    for query, gold in (*dataset("dev"), *dataset("test")):
        allowed = {event.id: event for event in eligible(query)}
        assert set(gold.relevant) <= set(allowed)
        assert all(allowed[identifier].context == gold.context and allowed[identifier].action == gold.action
                   for identifier in gold.relevant)
        assert gold.relevant or gold.action == "ask_user"
        assert not {"concise", "detailed", "bullets", "table"} & set(query.facets)
        assert len({event.id for event in query.events}) == len(query.events)


@pytest.mark.parametrize("beta", BETAS)
def test_modern_update_matches_energy_and_does_not_renormalize(beta: float) -> None:
    rng = np.random.default_rng(20)
    keys = rng.normal(size=(21, DIMENSION))
    keys /= np.linalg.norm(keys, axis=1, keepdims=True)
    prior = rng.normal(size=21)
    state = rng.normal(size=DIMENSION)

    def energy(value: np.typing.NDArray[np.float64]) -> float:
        logits = beta * (keys @ value) + prior
        peak = logits.max()
        return float(value @ value / 2 - (peak + np.log(np.exp(logits - peak).sum())) / beta)

    for _ in range(6):
        logits = beta * (keys @ state) + prior
        weights = np.exp(logits - logits.max())
        weights /= weights.sum()
        updated = state_update(keys, state, prior, beta)
        np.testing.assert_allclose(updated, weights @ keys, atol=1e-12)
        assert energy(updated) <= energy(state) + 1e-12
        state = updated


@pytest.mark.parametrize("arm", ARMS)
def test_common_lifecycle_and_empty_selection(arm: str) -> None:
    cases = stream("contract", 0)
    dev = stream("development-fixture", 0)
    encoder = Encoder("concept", 13, dev)
    for query, _ in cases:
        if query.id.rsplit("/", 1)[-1] not in ("expiry", "deletion", "unknown"):
            continue
        assert not observable_candidates(query)
        prepared = Prepared(query, encoder, 0.0)
        assert select(prepared, arm, Parameters(), GhostCache()) == ()


def test_real_ghost_supersession_edges_and_cold_replay() -> None:
    cases = stream("contract", 1)
    before = next(q for q, _ in cases if q.updates == 0)
    after, gold = next((q, g) for q, g in cases if q.updates == 1)
    cache = GhostCache()
    old = cache.get(before)
    new = cache.get(after)
    assert old.edges and new.edges
    assert old.hint_weights and new.hint_weights
    context_key = "style_preference:reply_mode:" + gold.context
    active = [node for node in new.nodes if node.conflict_key == context_key and node.status == "active"]
    assert len(active) == 1 and active[0].value_key == gold.action
    assert any(node.status == "superseded" and node.conflict_key == context_key for node in new.nodes)
    assert cache.get(after) is new
    assert cache.builds == 2


def test_real_store_deletion_probe(tmp_path: Path) -> None:
    assert deletion_probe(tmp_path / "store")["passed"] is True


@pytest.mark.parametrize("kind", ("concept", "text"))
def test_same_relevance_gate_and_hard_budget(kind: str) -> None:
    cases = stream("contract", 2)
    encoder = Encoder(kind, 31, stream("development-fixture", 0))
    cache = GhostCache()
    for query, _ in cases[:4]:
        prepared = Prepared(query, encoder, 0.3)
        accepted = {event.id for event in prepared.events}
        for arm in ARMS:
            events = select(prepared, arm, Parameters(), cache)
            assert {event.id for event in events} <= accepted
            context, fitted = render(events)
            assert len(context) <= BUDGET
            assert len(fitted) <= 2


def test_text_encoding_ignores_oracle_annotations_and_values() -> None:
    encoder = Encoder("text", 59, stream("development-fixture", 0))
    a = encoder.vector("原始观察文本", ("研究",), "concise")
    b = encoder.vector("原始观察文本", ("不同标签",), "detailed")
    np.testing.assert_array_equal(a, b)


def test_gold_does_not_affect_selection_or_model_payload() -> None:
    query, gold = stream("contract", 3)[0]
    prepared = Prepared(query, Encoder("concept", 13, stream("development-fixture", 0)), 0.0)
    events = select(prepared, "hopfield", Parameters(), GhostCache())
    payload = live.body(query, events, "model")
    study.metrics(query, Gold("other", "ask_user", ()), events)
    study.metrics(query, gold, events)
    assert payload == live.body(query, events, "model")
    schema = json.loads(payload)["response_format"]["json_schema"]["schema"]
    assert set(schema["properties"]["source"]["enum"]) == {"none", *(event.id for event in events)}


def test_accidental_correct_action_without_relevant_source_fails() -> None:
    query, gold = stream("contract", 4)[0]
    wrong = next(event for event in query.events if event.context != gold.context)
    assert not live.grounded(gold.action, wrong.id, (wrong,), gold)
    assert not live.grounded(gold.action, "none", (), gold)
    relevant = next(event for event in query.events if event.id in gold.relevant)
    assert live.grounded(gold.action, relevant.id, (relevant,), gold)
    absent = Gold("unknown", "ask_user", ())
    assert live.grounded("ask_user", "none", (), absent)


@pytest.mark.parametrize("raw", ["{}", '{"action":"concise","source":"invented"}',
                                     '{"action":"concise","source":"none","extra":1}',
                                     '```json\n{"action":"ask_user","source":"none"}\n```'])
def test_strict_source_interface(raw: str) -> None:
    assert live.decode(raw, ()) is None


def test_transport_error_is_saved_once(tmp_path: Path) -> None:
    with (
        patch.object(urllib.request, "urlopen", side_effect=urllib.error.URLError("injected")) as sender,
        pytest.raises(urllib.error.URLError),
    ):
        live.generate("http://127.0.0.1:1/v1", b"{}", (), tmp_path / "request")
    assert sender.call_count == 1
    assert (tmp_path / "request/failure.json").exists()
    assert (tmp_path / "request/request.json").read_bytes() == b"{}"


def test_bootstrap_clusters_are_not_query_count() -> None:
    result = study.bootstrap([0.0] * 12)
    assert result["clusters"] == 12
    assert result["lower"] == result["upper"] == 0
    assert result["interpretation"] == "EXCLUDES_FIVE_POINT_GAIN_ON_THIS_GENERATOR"


def test_selection_lock_rejects_post_development_changes(tmp_path: Path) -> None:
    data: dict[str, object] = {"selection": {}}
    study.dump(tmp_path / "development.json", data)
    study.dump(tmp_path / "development-lock.json", {"sha256": "wrong"})
    with pytest.raises(ValueError, match="changed after lock"):
        study.selections(tmp_path)


def test_expiry_and_deletion_have_real_before_after_states() -> None:
    cases = {query.id.rsplit("/", 1)[-1]: query for query, _ in stream("contract", 5)}
    assert observable_candidates(cases["expiry-before"])
    assert not observable_candidates(cases["expiry"])
    assert observable_candidates(cases["deletion-before"])
    assert not observable_candidates(cases["deletion"])
    before = cases["deletion-before"]
    revoked = replace(before, events=tuple(replace(event, deleted=True) for event in before.events))
    assert not eligible(revoked)


def test_incomplete_and_noisy_cues_are_identifiable_before_encoding() -> None:
    for number in range(96):
        for query, gold in stream("test", number)[:2]:
            by_context = {event.context: event.facets for event in observable_candidates(query)}
            overlap = {context: len(set(facets) & set(query.facets)) for context, facets in by_context.items()}
            assert overlap[gold.context] > max(value for key, value in overlap.items() if key != gold.context)


def test_small_development_lock_and_heldout_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unchecked(home: Path) -> None:
        return None

    def miniature(split: str) -> tuple[tuple[Query, Gold], ...]:
        return stream("contract-mini-" + split, 0)

    monkeypatch.setattr(study, "check", unchecked)
    monkeypatch.setattr(study, "dataset", miniature)
    study.develop(tmp_path)
    assert study.selections(tmp_path)
    study.test(tmp_path)
    result = json.loads((tmp_path / "heldout-result.json").read_bytes())
    assert result["logical_cells"] == 14 * 3 * 2 * 8
    assert result["deletion_probe"]["passed"] is True
    for encoding in ("concept", "text"):
        assert result["summary"][encoding]["comparisons"]
        assert result["adaptation"][encoding]["hopfield"]["stream_seed_pairs"] == 3
