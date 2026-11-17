"""Frozen public-producer matrix for record-only Hebbian control payloads."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.ghost.hebbian import GhostHebbianStore
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.payload_types import object_fields
from tests.test_ghost_graph_tie_replay import NOW, _accepted

NEXT = "2026-11-19T00:00:00Z"
KINDS = ("candidate_removed", "scope_deleted", "state_decayed")
VARIANTS = (
    "valid", "missing_payload", "scalar_payload", "missing_count", "boolean_count",
    "negative_count", "string_count", "float_count", "extra_key",
)


def _seed(home: Path, scope: str, kind: str) -> GhostHebbianStore:
    inbox, store = GhostInboxStore(home), GhostHebbianStore(home)
    with patch("codey.ghost._common.now_iso_z", return_value=NOW):
        peer, hub = (_accepted(inbox, home, scope, index) for index in range(2))
        assert store.reinforce_candidate(peer).applied, "INCOMPLETE_FIXTURE: peer"
        assert store.reinforce_candidate(hub, related_candidates=(peer,)).applied, "INCOMPLETE_FIXTURE: hub"
        nodes = store.list_nodes()
        assert len(nodes) == 2 and len(store.list_edges()) == 1, "INCOMPLETE_FIXTURE: graph"
        assert all(node.scope == scope and node.scope_ref == hub.scope_ref for node in nodes)
        expected: dict[str, object]
        if kind == "candidate_removed":
            rejected = inbox.review_candidate(peer.id, "reject", reviewed_by="test")
            assert rejected is not None and rejected.status == "rejected", "INCOMPLETE_FIXTURE: review"
            results = store.sync_from_inbox(inbox)
            assert any(result.reason == "removed_rejected_candidate" for result in results)
            expected = {"candidate_id": peer.id, "removed_nodes": 1, "removed_edges": 1}
            assert len(store.list_nodes()) == 1 and not store.list_edges()
        elif kind == "scope_deleted":
            assert store.delete_scope(scope, project=str(home / "project"), session_id="s1") == {
                "nodes": 2, "edges": 1,
            }
            expected = {"scope": scope, "scope_ref": hub.scope_ref, "removed_nodes": 2, "removed_edges": 1}
            assert not store.list_nodes() and not store.list_edges()
        else:
            assert kind == "state_decayed"
            result = store.decay(maintenance_as_of=NEXT)
            assert result == {
                "removed_nodes": 0, "removed_edges": 0, "decayed_nodes": 2, "decayed_edges": 1,
                "skipped_reason": "",
            }, ("INCOMPLETE_FIXTURE: decay", result)
            expected = {"removed_nodes": 0, "removed_edges": 0, "decayed_nodes": 2, "decayed_edges": 1}
            assert len(store.list_nodes()) == 2 and len(store.list_edges()) == 1
    [event] = [event for event in store.graph.events() if event["type"] == "ghost_hebbian_" + kind]
    assert event["payload"] == expected, ("INCOMPLETE_FIXTURE: control", event)
    assert event["ts"] == (NEXT if kind == "state_decayed" else NOW)
    return store


def _replace_payload(store: GhostHebbianStore, kind: str, variant: str) -> None:
    if variant == "valid":
        return
    [event] = [event for event in store.graph.events() if event["type"] == "ghost_hebbian_" + kind]
    payload = event["payload"]
    assert object_fields(payload)
    changed = dict(event)
    broken = dict(payload)
    if variant == "missing_payload":
        changed.pop("payload")
    elif variant == "scalar_payload":
        changed["payload"] = 17
    else:
        if variant == "missing_count":
            broken.pop("removed_nodes")
        elif variant == "extra_key":
            broken["unexpected"] = 0
        elif variant == "legacy":
            assert kind == "state_decayed"
            broken.pop("decayed_nodes")
            broken.pop("decayed_edges")
        else:
            corrupt_counts: dict[str, object] = {
                "boolean_count": True, "negative_count": -1, "string_count": "0", "float_count": 0.0,
            }
            broken["removed_nodes"] = corrupt_counts[variant]
        changed["payload"] = broken
    # Alter only the committed JSON payload; identity, ordering, cursors and
    # the last valid projection remain owned by the original public producer.
    with store.graph.database.connection() as connection:
        matched = connection.execute(
            "UPDATE graph_events SET payload=? WHERE graph='hebbian' AND payload=?",
            (json.dumps(changed, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
             json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        )
        assert matched.rowcount == 1, "INCOMPLETE_FIXTURE: injection target"


def _measure(home: Path, scope: str, restart: str, kind: str, variant: str) -> None:
    store = _seed(home, scope, kind)
    before, stats = store.graph.rows(), store.graph.event_stats()
    _replace_payload(store, kind, variant)
    history = store.graph.events()
    if restart == "cold":
        store = GhostHebbianStore(home)
    accepted: list[bool] = []
    projections: list[bool] = []
    preserved: list[bool] = []
    with patch("codey.ghost._common.now_iso_z", side_effect=AssertionError("replay read wall clock")):
        for _ in range(2):
            accepted.append(store.rebuild_from_events())
            projections.append(store.graph.rows() == before)
            preserved.append(store.graph.events() == history)
    measurement: dict[str, object] = {
        "scope": scope, "restart": restart, "kind": kind, "variant": variant, "fixture_valid": True,
        "accepted": accepted, "projection_unchanged": projections, "history_unchanged": preserved,
        "event_count_unchanged": store.graph.event_stats()["events"] == stats["events"],
    }
    print("HEBBIAN_CONTROL_CELL " + json.dumps(measurement, sort_keys=True))
    assert accepted == [variant in {"valid", "legacy"}] * 2
    assert all(projections), "replay changed last valid projection"
    assert all(preserved), "replay rewrote original audit log"
    assert measurement["event_count_unchanged"] is True


@pytest.mark.parametrize("scope", ("user", "project", "session"))
@pytest.mark.parametrize("restart", ("warm", "cold"))
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("variant", VARIANTS)
def test_committed_control_replay_preserves_projection_and_rejects_malformed_payload(
    tmp_path: Path, scope: str, restart: str, kind: str, variant: str,
) -> None:
    _measure(tmp_path, scope, restart, kind, variant)


@pytest.mark.parametrize("scope", ("user", "project", "session"))
@pytest.mark.parametrize("restart", ("warm", "cold"))
def test_original_schema_one_two_counter_decay_remains_replayable(
    tmp_path: Path, scope: str, restart: str,
) -> None:
    _measure(tmp_path, scope, restart, "state_decayed", "legacy")
