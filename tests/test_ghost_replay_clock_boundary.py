"""Fixed persisted Affinity events must not acquire wall-clock semantics on rebuild."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Self
from unittest.mock import patch

import pytest

from codey.ghost import graph_primitives
from codey.ghost.affinity import GhostAffinityStore
from codey.ghost.affinity_events import (
    _decay_applied_event,
    _edge_reinforced_event,
    _node_reinforced_event,
    _snapshot_event,
    replay_affinity_events,
)
from codey.ghost.affinity_model import AffinityEdgeSpec, AffinityNodeSpec, _node_id
from codey.ghost.affinity_sources import _scope_ref

BASE = "2026-01-01T00:00:00Z"
AS_OF = "2026-04-01T00:00:00Z"
CLOCKS = ("2026-01-01T00:00:00+00:00", "2027-01-01T00:00:00+00:00")


@dataclass(frozen=True)
class ReplayResult:
    accepted: bool
    digest: str
    nodes: tuple[tuple[str, float, str], ...]
    edges: tuple[tuple[str, float, str], ...]

    def payload(self) -> dict[str, object]:
        return {"accepted": self.accepted, "digest": self.digest, "nodes": self.nodes, "edges": self.edges}


def _fixed_datetime(clock: str) -> type[datetime]:
    instant = datetime.fromisoformat(clock)

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            selected = instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)
            return cls.fromisoformat(selected.isoformat())

    return FixedDatetime


def _spec(key: str, scope: str, scope_ref: str, source: str = "first") -> AffinityNodeSpec:
    return AffinityNodeSpec(
        kind="research_concept", key=key, label=f"Concept {key}", scope=scope, scope_ref=scope_ref,
        confidence=0.8, reward=1.0, source_refs=(f"research_interest:{key}:{source}",), evidence_refs=(),
    )


def _events(scope: str, scope_ref: str, variant: str) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    scope_ref = _scope_ref(scope, scope_ref)
    initial = [_node_reinforced_event(_spec(key, scope, scope_ref), ts=BASE) for key in ("alpha", "beta")]
    initial.append(_edge_reinforced_event(AffinityEdgeSpec(
        source=_node_id("research_concept", scope, scope_ref, "alpha"),
        target=_node_id("research_concept", scope, scope_ref, "beta"),
        relation="associated_with", scope=scope, scope_ref=scope_ref,
        confidence=0.8, reward=1.0, source_refs=("research_interest:alpha:beta",), proof_refs=(),
    ), ts=BASE))
    if variant == "snapshot_basis":
        nodes, edges = replay_affinity_events(initial)
        nodes[0] = replace(nodes[0], last_decayed_at="not-a-date")
        suffix = [_snapshot_event(nodes, edges, ts=BASE, reason="clock_boundary_fixture")]
    else:
        timestamp = "not-a-date" if variant == "reinforcement" else BASE
        suffix = [_node_reinforced_event(_spec("alpha", scope, scope_ref, "second"), ts=timestamp)]
    suffix.append(_decay_applied_event(
        removed_nodes=0, removed_edges=0, decayed_nodes=2, decayed_edges=1, min_interval_seconds=0,
        ts="not-a-date" if variant == "decay" else AS_OF,
    ))
    return initial, suffix


def _rebuild(store: GhostAffinityStore, clock: str, scope: str, scope_ref: str) -> ReplayResult:
    with store.graph.transaction(write=False):
        before = store.graph.rows()
        events = store.graph.events()
    with patch.object(graph_primitives, "datetime", _fixed_datetime(clock)):
        try:
            assert store.rebuild_from_events()
        except ValueError:
            accepted = False
        else:
            accepted = True
    with store.graph.transaction(write=False):
        after = store.graph.rows()
        assert store.graph.events() == events, "rebuild must preserve original event history"
    if not accepted:
        assert after == before, "rejected replay must roll back its projection"
    project = scope_ref if scope == "project" else ""
    session_id = scope_ref if scope == "session" else ""
    nodes = store.list_nodes(scope=scope, project=project, session_id=session_id)
    edges = store.list_edges(scope=scope, project=project, session_id=session_id)
    assert nodes and edges, "valid prior projection remains available"
    digest = hashlib.sha256(json.dumps(after, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return ReplayResult(
        accepted, digest, tuple((node.id, node.weight, node.status) for node in nodes),
        tuple((edge.id, edge.weight, edge.status) for edge in edges),
    )


@pytest.mark.parametrize("scope", ("user", "project", "session"))
@pytest.mark.parametrize("variant", ("valid", "reinforcement", "decay", "snapshot_basis"))
def test_same_persisted_events_rebuild_identically_or_reject_without_wall_clock(
    tmp_path: Path, scope: str, variant: str,
) -> None:
    scope_ref = str((tmp_path / "project").resolve()) if scope == "project" else "s1" if scope == "session" else ""
    initial, suffix = _events(scope, scope_ref, variant)
    nodes, edges = replay_affinity_events(initial)
    store = GhostAffinityStore(tmp_path)
    # Fault injection: preserve a known valid projection while the durable log contains
    # a malformed timestamp. Do not claim that a normal producer emits this fixture.
    with store.graph.transaction():
        store.graph.append((*initial, *suffix))
        store.graph.replace_rows((node.to_payload() for node in nodes), (edge.to_payload() for edge in edges))
    first = _rebuild(store, CLOCKS[0], scope, scope_ref)
    second = _rebuild(GhostAffinityStore(tmp_path), CLOCKS[1], scope, scope_ref)
    measurement: dict[str, object] = {
        "scope": scope, "variant": variant, "first": first.payload(), "second": second.payload(),
    }
    print("REPLAY_CELL " + json.dumps(measurement, sort_keys=True))
    if variant == "valid":
        assert first.accepted and second.accepted
        assert first.edges[0][1] != edges[0].weight, "valid control must perform a real transition"
    assert first == second, "the same durable events acquired wall-clock-dependent projection semantics"


def test_clock_fixture_uses_real_utc_instants() -> None:
    assert _fixed_datetime(CLOCKS[0]).now(UTC) == datetime(2026, 1, 1, tzinfo=UTC)
