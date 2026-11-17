"""Corrupt committed Hebbian events cannot silently revive older preferences."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.ghost.directive import build_ghost_directive
from codey.ghost.hebbian import GhostHebbianStore
from codey.ghost.inbox import GhostInboxStore, GhostMemoryCandidate
from codey.ghost.payload_types import object_fields
from tests.test_ghost_hebbian import _accept, _ingest_one, _signal

NOW = "2026-11-17T00:00:00Z"
VARIANTS = (
    "valid", "superseded_node", "upsert_node", "edge", "unknown_type", "schema_version",
    "boolean_schema", "timestamp", "deletion_payload",
)


def _seed(home: Path, scope: str) -> GhostHebbianStore:
    inbox = GhostInboxStore(home)
    store = GhostHebbianStore(home)
    project = str(home / "project")
    candidates: list[GhostMemoryCandidate] = []
    for index, value in enumerate(("concise", "detailed")):
        candidate = _accept(inbox, _ingest_one(
            inbox, _signal(
                scope=scope, summary=f"Prefer {value} answers.", quote=f"Use {value} answers.",
                confidence=1.0, conflict_key="reply_length", value_key=value,
            ), project=project, run_id="same-run",
        ))
        result = store.reinforce_candidate(candidate, related_candidates=tuple(candidates))
        assert result.applied, (index, result)
        candidates.append(candidate)
    assert {node.status for node in store.list_nodes()} == {"active", "superseded"}
    assert [node.value_key for node in store.list_nodes(status="active")] == ["detailed"]
    assert len(store.list_edges()) == 1
    return store


def _corrupt(store: GhostHebbianStore, variant: str) -> None:
    if variant == "valid":
        return
    target_type = {
        "upsert_node": "ghost_hebbian_node_upsert", "edge": "ghost_hebbian_edge_upsert",
    }.get(variant, "ghost_hebbian_node_superseded")
    [event] = [row for row in store.graph.events() if row["type"] == target_type][-1:]
    changed = dict(event)
    if variant in {"superseded_node", "upsert_node", "edge"}:
        field = "edge" if variant == "edge" else "node"
        payload = changed[field]
        assert object_fields(payload)
        broken = dict(payload)
        broken.pop("relation" if variant == "edge" else "label")
        changed[field] = broken
    elif variant == "unknown_type":
        changed["type"] = "ghost_hebbian_future_transition"
    elif variant == "schema_version":
        changed["schema_version"] = 999
    elif variant == "boolean_schema":
        changed["schema_version"] = True
    elif variant == "timestamp":
        changed["ts"] = "not-a-date"
    else:
        assert variant == "deletion_payload"
        deletion_payload: dict[str, object] = {}
        changed = {"schema_version": 1, "type": "ghost_hebbian_scope_deleted", "ts": NOW, "payload": deletion_payload}
    # Deliberate disk-corruption injection, retaining event identity, sequence,
    # cursor and last valid projection. No normal producer emits these rows.
    with store.graph.database.connection() as connection:
        matched = connection.execute(
            "UPDATE graph_events SET payload=? WHERE graph='hebbian' AND payload=?",
            (json.dumps(changed, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
             json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        )
        assert matched.rowcount == 1


@pytest.mark.parametrize("scope", ("user", "project", "session"))
@pytest.mark.parametrize("restart", ("warm", "cold"))
@pytest.mark.parametrize("variant", VARIANTS)
def test_bad_hebbian_event_rejects_rebuild_and_preserves_last_valid_projection(
    tmp_path: Path, scope: str, restart: str, variant: str,
) -> None:
    with patch("codey.ghost._common.now_iso_z", return_value=NOW):
        store = _seed(tmp_path, scope)
        before = store.graph.rows()
        directive = build_ghost_directive(store, project=str(tmp_path / "project"), session_id="s1")
        assert directive.text and len(directive.selected_nodes) == 1
        assert directive.selected_nodes[0].value_key == "detailed"
        _corrupt(store, variant)
        events = store.graph.events()
        if restart == "cold":
            store = GhostHebbianStore(tmp_path)
        accepted: list[bool] = []
        for _ in range(2):
            accepted.append(store.rebuild_from_events())
            assert store.graph.events() == events, "rebuild changed original audit history"
        unchanged = store.graph.rows() == before
        active = tuple(node.value_key for node in store.list_nodes(status="active"))
        same_directive = build_ghost_directive(
            store, project=str(tmp_path / "project"), session_id="s1",
        ) == directive
        measurement: dict[str, object] = {
            "scope": scope, "restart": restart, "variant": variant, "accepted": accepted,
            "projection_unchanged": unchanged, "active": active, "directive_unchanged": same_directive,
        }
        print("HEBBIAN_REPLAY_CELL " + json.dumps(measurement, sort_keys=True))
        assert accepted == [variant == "valid"] * 2
        assert unchanged, "bad replay replaced the last valid projection"
        assert active == ("detailed",), "replay revived a superseded preference"
        assert same_directive, "replay changed the normal directive consumer"
