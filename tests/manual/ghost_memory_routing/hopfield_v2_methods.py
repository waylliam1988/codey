"""Shared eligibility/abstention, modern Hopfield variants, real Ghost state adapter."""

from __future__ import annotations

import math
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import patch

import numpy as np

from codey.ghost.affinity import GhostAffinityStore
from codey.ghost.directive import MIN_DIRECTIVE_WEIGHT, _node_sort_key, _preview_decayed_node, _resolve_competing_nodes
from codey.ghost.hebbian import GhostEdge, GhostHebbianStore, GhostNode
from codey.ghost.inbox import GhostMemoryCandidate
from codey.ghost.observation_index import score_observation, tokenize
from tests.manual.ghost_memory_routing.hopfield_v2_data import (
    Encoder,
    Event,
    Parameters,
    Query,
    Vector,
    eligible,
    observable_candidates,
    render,
)


def timestamp(day: int) -> str:
    return (datetime(2026, 9, 1, tzinfo=UTC) + timedelta(days=day)).isoformat().replace("+00:00", "Z")


def candidate(event: Event) -> GhostMemoryCandidate:
    # Conditional conflict keys are an explicit HARNESS adapter. Production's
    # automatic extraction/typed renderer do not gain these arbitrary contexts.
    return GhostMemoryCandidate(
        id=event.id, candidate_type="preference", signal_kind="style_preference", status="accepted",
        scope="project", summary=event.text, evidence_quote=event.text, confidence=0.9,
        conflict_key="style_preference:reply_mode:" + event.context,
        value_key=event.action, session_id="synthetic", run_id=f"batch-{event.batch}", project=event.project,
        created_at=timestamp(event.day), updated_at=timestamp(event.day),
        gate_reason="manual_accept" if event.explicit else "fixture_accepted_feedback",
        reviewed_at=timestamp(event.day) if event.explicit else "", evidence_refs=("fixture:" + event.id,),
    )


@dataclass(frozen=True)
class GhostSnapshot:
    nodes: tuple[GhostNode, ...]
    edges: tuple[GhostEdge, ...]
    hint_weights: tuple[tuple[str, float], ...]


class GhostCache:
    """Each distinct history actually runs production SQLite state transitions.

    Stores are temporary and never use the user's state. Exported snapshots
    retain real weights, status, candidate provenance and affinity order hints.
    """

    def __init__(self) -> None:
        self.cache: dict[tuple[Event, ...], GhostSnapshot] = {}
        self.builds = 0

    def get(self, query: Query) -> GhostSnapshot:
        history = tuple(sorted(eligible(query), key=lambda event: (event.day, event.id)))
        if history in self.cache:
            return self.cache[history]
        with tempfile.TemporaryDirectory(prefix="ghost-hopfield-v2-") as directory:
            store = GhostHebbianStore(directory)
            by_batch: dict[int, list[GhostMemoryCandidate]] = {}
            with store.graph.transaction():
                for event in history:
                    observed = candidate(event)
                    with patch("codey.ghost._common.now_iso_z", return_value=timestamp(event.day)):
                        result = store.reinforce_candidate(observed, related_candidates=by_batch.get(event.batch, ()))
                    if not result.applied:
                        raise RuntimeError("production Ghost reinforcement failed: " + result.reason)
                    by_batch.setdefault(event.batch, []).append(observed)
            affinity = GhostAffinityStore(directory)
            with patch("codey.ghost._common.now_iso_z", return_value=timestamp(query.day)):
                synced = affinity.sync_from_sources(hebbian_store=store, maintenance_as_of=timestamp(query.day))
                if not synced.ok:
                    raise RuntimeError("production affinity sync failed: " + synced.skipped_reason)
                nodes = store.list_nodes()
                hints = affinity.query_directive_order_hints(nodes, project=query.project)
            snapshot = GhostSnapshot(nodes, store.list_edges(), tuple((hint.target, hint.weight * hint.confidence)
                                                                     for hint in hints))
            # Cold reopen + replay must preserve the actual persisted graph.
            reopened = GhostHebbianStore(directory)
            if reopened.list_nodes() != snapshot.nodes or not reopened.rebuild_from_events():
                raise RuntimeError("production graph reopen/replay failed")
            if reopened.list_nodes() != snapshot.nodes:
                raise RuntimeError("production graph replay changed nodes")
        self.cache[history] = snapshot
        self.builds += 1
        return snapshot


class Prepared:
    def __init__(self, query: Query, encoder: Encoder, threshold: float) -> None:
        self.query = query
        self.encoder = encoder
        original = observable_candidates(query)
        self.vector = encoder.vector(query.text, query.facets)
        keys = np.array([encoder.vector(event.text, event.facets, event.action) for event in original],
                        dtype=np.float64)
        similarities = keys @ self.vector if len(original) else np.array([], dtype=np.float64)
        self.events = tuple(event for event, score in zip(original, similarities, strict=True) if score >= threshold)
        self.keys = np.array([encoder.vector(event.text, event.facets, event.action) for event in self.events],
                             dtype=np.float64)
        self.prior = np.array([-(query.day - event.day) / 45.0 for event in self.events], dtype=np.float64)
        self.weights: Vector | None = None


def state_update(keys: Vector, state: Vector, prior: Vector, beta: float) -> Vector:
    logits = beta * (keys @ state) + prior
    weights = np.exp(logits - logits.max())
    weights /= weights.sum()
    return cast(Vector, keys.T @ weights)


def ghost_rank(prepared: Prepared, cache: GhostCache) -> tuple[Event, ...]:
    snapshot = cache.get(prepared.query)
    allowed = {event.id: event for event in prepared.events}
    # The adapter supplies query relevance; production owns learned weights,
    # explicit supersession, competing-value abstention and directive ordering.
    decayed = (_preview_decayed_node(node, now=timestamp(prepared.query.day)) for node in snapshot.nodes)
    nodes = tuple(node for node in decayed if node.status == "active" and not node.superseded_by
                  and node.weight >= MIN_DIRECTIVE_WEIGHT
                  and set(node.candidate_ids) & set(allowed))
    resolved = _resolve_competing_nodes(nodes, warnings=[])
    hints = dict(snapshot.hint_weights)
    ranked_nodes = sorted(resolved, key=lambda node: _node_sort_key(node, hints))
    result: list[Event] = []
    for node in ranked_nodes:
        origins = [allowed[identifier] for identifier in node.candidate_ids if identifier in allowed]
        origins.sort(key=lambda event: (-event.day, event.id))
        if origins:
            result.append(origins[0])
    return tuple(result)


def select(prepared: Prepared, arm: str, parameters: Parameters, cache: GhostCache) -> tuple[Event, ...]:
    if not prepared.events or arm == "off":
        return ()
    if arm == "ghost_adapter":
        return render(ghost_rank(prepared, cache))[1]
    events, keys, prior = prepared.events, prepared.keys, prepared.prior
    if arm == "fixed":
        keep = [index for index, event in enumerate(events) if event.calibration]
        events = tuple(events[index] for index in keep)
        keys, prior = keys[keep], prior[keep]
        if not events:
            return ()
    state = prepared.vector.copy()
    if arm in ("hopfield", "anchored"):
        for _ in range(parameters.steps):
            state = state_update(keys, state, prior, parameters.beta)
            if arm == "anchored":
                state = 0.25 * prepared.vector + 0.75 * state
    elif arm == "linear":
        if prepared.weights is None:
            prepared.weights = keys.T @ keys / len(events)
        for _ in range(parameters.steps):
            state = prepared.vector + prepared.weights @ state
            state /= max(math.sqrt(float(state @ state)), 1e-12)
    scores = parameters.beta * (keys @ state) + prior
    if arm == "latest":
        scores = np.array([event.day for event in events], dtype=np.float64)
    elif arm == "lexical":
        scores = np.array([score_observation(tokenize(prepared.query.text), {
            "user_text": event.text, "ts": timestamp(event.day),
        }, datetime.fromisoformat(timestamp(prepared.query.day).replace("Z", "+00:00")).timestamp())
                           for event in events])
    order = sorted(range(len(events)), key=lambda index: (-float(scores[index]), events[index].id))
    return render(tuple(events[index] for index in order if arm != "lexical" or scores[index] > 0))[1]


def deletion_probe(directory: Path) -> dict[str, object]:
    """Independent real-store deletion/reopen/replay check, not accuracy evidence."""
    from tests.manual.ghost_memory_routing.hopfield_v2_data import stream

    query = stream("probe", 0)[0][0]
    event = eligible(query)[0]
    store = GhostHebbianStore(directory)
    accepted = candidate(event)
    assert store.reinforce_candidate(accepted).applied
    assert store.list_nodes()
    removed = store.remove_candidate(accepted)
    reopened = GhostHebbianStore(directory)
    assert not reopened.list_nodes()
    assert reopened.rebuild_from_events()
    assert not reopened.list_nodes()
    return {"passed": True, "removed": removed, "reopen_empty": True, "replay_empty": True}
