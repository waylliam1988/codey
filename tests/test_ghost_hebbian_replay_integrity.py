"""Corrupt committed Hebbian events cannot silently revive older preferences."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.directive import build_ghost_directive
from codey.ghost.hebbian import GhostEdge, GhostHebbianStore, GhostNode
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
        "upsert_node": "ghost_hebbian_node_upsert", "upsert_status": "ghost_hebbian_node_upsert",
        "edge": "ghost_hebbian_edge_upsert",
    }.get(variant, "ghost_hebbian_node_superseded")
    [event] = [row for row in store.graph.events() if row["type"] == target_type][-1:]
    changed = dict(event)
    if variant in {"superseded_node", "upsert_node", "edge", "superseded_status", "upsert_status"}:
        field = "edge" if variant == "edge" else "node"
        payload = changed[field]
        assert object_fields(payload)
        broken = dict(payload)
        if variant.endswith("_status"):
            broken["status"] = "superseded" if variant == "upsert_status" else "active"
        else:
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


@pytest.mark.parametrize("scope", ("user", "project", "session"))
@pytest.mark.parametrize("variant", ("valid", "superseded_node"))
@pytest.mark.parametrize("confirmed", (False, True))
def test_normal_cli_rebuild_reports_rejection_without_an_enable_flag(
    tmp_path: Path, scope: str, variant: str, confirmed: bool,
) -> None:
    store = _seed(tmp_path, scope)
    before = store.graph.rows()
    _corrupt(store, variant)
    events = store.graph.events()
    args = [sys.executable, "-m", "codey", "ghost", "rebuild-state", "--state-home", str(tmp_path)]
    if confirmed:
        args.append("--yes")
    completed = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert completed.returncode == (2 if not confirmed else 0 if variant == "valid" else 1), completed.stderr
    payload: object = json.loads(completed.stdout)
    assert object_fields(payload)
    if confirmed:
        assert payload["ok"] is (variant == "valid")
    else:
        assert payload["error"] == "rebuild-state requires --yes"
    assert store.graph.rows() == before and store.graph.events() == events


@pytest.mark.parametrize("scope", ("user", "project", "session"))
@pytest.mark.parametrize("action", ("delete_scope", "reset_all"))
def test_rejected_replay_does_not_block_explicit_deletion_or_resurrect_after_reopen(
    tmp_path: Path, scope: str, action: str,
) -> None:
    store = _seed(tmp_path, scope)
    _corrupt(store, "superseded_node")
    history = store.graph.events()
    assert not store.rebuild_from_events()
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert surface.inbox is not None
    for control in ("accept_events", "retrieve_projections", "run_maintenance", "admit_hints"):
        assert surface.inbox.set_control(control, False, scope="user")
    status, payload = surface.dispatch_action({
        "action": action, "scope": scope, "project": str(tmp_path / "project"),
        "session_id": "s1", "confirm": True,
    })
    assert status == 200 and payload["ok"] is True, payload
    reopened = GhostHebbianStore(tmp_path)
    assert not reopened.list_nodes() and not reopened.list_edges()
    # Reset advances the active window; scope deletion retains its earlier
    # corrupt event, so a subsequent rebuild still rejects that history.
    assert reopened.rebuild_from_events() is (action == "reset_all")
    assert not reopened.list_nodes() and not reopened.list_edges()
    with reopened.database.connection() as connection:
        rows: Iterable[sqlite3.Row] = connection.execute(
            "SELECT payload FROM graph_events WHERE graph='hebbian' ORDER BY sequence",
        )
        raw: list[dict[str, object]] = []
        for row in rows:
            encoded: object = row[0]
            assert isinstance(encoded, str)
            decoded: object = json.loads(encoded)
            assert object_fields(decoded)
            raw.append(decoded)
    assert raw[:len(history)] == history, "explicit deletion rewrote the corrupt audit history"


@pytest.mark.parametrize("scope", ("user", "project", "session"))
def test_failed_projection_write_rolls_back_and_a_later_rebuild_recovers(tmp_path: Path, scope: str) -> None:
    store = _seed(tmp_path, scope)
    before, events = store.graph.rows(), store.graph.events()

    def fail_after_mutation(nodes: Iterable[GhostNode], edges: Iterable[GhostEdge]) -> None:
        assert tuple(nodes)
        assert tuple(edges)
        store.graph.replace_rows((), ())
        raise sqlite3.OperationalError("injected after deleting graph rows")

    with patch.object(store, "_write_state", side_effect=fail_after_mutation):
        assert not store.rebuild_from_events()
    assert store.graph.rows() == before and store.graph.events() == events
    with patch("codey.ghost._common.now_iso_z", side_effect=AssertionError("replay read the clock")):
        assert store.rebuild_from_events()
    assert store.graph.rows() == before and store.graph.events() == events


@pytest.mark.parametrize("scope", ("user", "project", "session"))
@pytest.mark.parametrize("variant", ("superseded_status", "upsert_status"))
def test_replay_rejects_node_status_that_disagrees_with_event_type(
    tmp_path: Path, scope: str, variant: str,
) -> None:
    store = _seed(tmp_path, scope)
    before = store.graph.rows()
    _corrupt(store, variant)
    history = store.graph.events()
    assert not store.rebuild_from_events()
    assert store.graph.rows() == before and store.graph.events() == history
