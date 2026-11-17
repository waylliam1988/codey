"""Control-field decoding, ordinary CLI and transactional recovery boundaries."""

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
from codey.ghost.hebbian import GhostEdge, GhostHebbianStore, GhostNode
from codey.ghost.payload_types import object_fields
from tests.test_ghost_hebbian_control_replay import KINDS, _replace_payload, _seed

COUNTERS = (
    ("candidate_removed", "removed_nodes"), ("candidate_removed", "removed_edges"),
    ("scope_deleted", "removed_nodes"), ("scope_deleted", "removed_edges"),
    ("state_decayed", "removed_nodes"), ("state_decayed", "removed_edges"),
    ("state_decayed", "decayed_nodes"), ("state_decayed", "decayed_edges"),
)


def _disk_history(store: GhostHebbianStore) -> tuple[tuple[int, str, str], ...]:
    with store.database.connection() as connection:
        rows: Iterable[sqlite3.Row] = connection.execute(
            "SELECT sequence,event_id,payload FROM graph_events WHERE graph='hebbian' ORDER BY sequence",
        )
        history: list[tuple[int, str, str]] = []
        for row in rows:
            sequence: object = row[0]
            identity: object = row[1]
            payload: object = row[2]
            assert type(sequence) is int and isinstance(identity, str) and isinstance(payload, str)
            history.append((sequence, identity, payload))
    return tuple(history)


def _payload(store: GhostHebbianStore, kind: str) -> dict[str, object]:
    [event] = [event for event in store.graph.events() if event["type"] == "ghost_hebbian_" + kind]
    payload = event["payload"]
    assert object_fields(payload)
    return dict(payload)


def _change(store: GhostHebbianStore, kind: str, payload: object) -> None:
    [event] = [event for event in store.graph.events() if event["type"] == "ghost_hebbian_" + kind]
    changed = {**event, "payload": payload}
    with store.database.connection() as connection:
        matched = connection.execute(
            "UPDATE graph_events SET payload=? WHERE graph='hebbian' AND payload=?",
            (json.dumps(changed, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
             json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        )
        assert matched.rowcount == 1


def _assert_rejected(store: GhostHebbianStore, home: Path) -> None:
    before, history = store.graph.rows(), _disk_history(store)
    for owner in (store, GhostHebbianStore(home)):
        assert not owner.rebuild_from_events()
        assert owner.graph.rows() == before and _disk_history(owner) == history


@pytest.mark.parametrize(("kind", "field"), COUNTERS)
@pytest.mark.parametrize("variant", ("missing", "boolean", "negative", "string", "float", "null"))
def test_every_recorded_counter_requires_a_nonnegative_json_integer(
    tmp_path: Path, kind: str, field: str, variant: str,
) -> None:
    store = _seed(tmp_path, "user", kind)
    payload = _payload(store, kind)
    if variant == "missing":
        payload.pop(field)
    else:
        invalid: dict[str, object] = {"boolean": False, "negative": -1, "string": "1", "float": 1.0, "null": None}
        payload[field] = invalid[variant]
    _change(store, kind, payload)
    _assert_rejected(store, tmp_path)


@pytest.mark.parametrize("scope", ("user", "project", "session"))
@pytest.mark.parametrize("variant", ("missing", "empty", "whitespace", "boolean", "integer", "null"))
def test_candidate_control_requires_a_recorded_identity(tmp_path: Path, scope: str, variant: str) -> None:
    store = _seed(tmp_path, scope, "candidate_removed")
    payload = _payload(store, "candidate_removed")
    if variant == "missing":
        payload.pop("candidate_id")
    else:
        invalid: dict[str, object] = {"empty": "", "whitespace": " ", "boolean": True, "integer": 1, "null": None}
        payload["candidate_id"] = invalid[variant]
    _change(store, "candidate_removed", payload)
    _assert_rejected(store, tmp_path)


@pytest.mark.parametrize("scope", ("user", "project", "session"))
@pytest.mark.parametrize("variant", ("missing_scope", "unknown_scope", "scalar_scope", "missing_ref", "scalar_ref", "bad_ref"))
def test_scope_control_requires_its_exact_scope_reference(tmp_path: Path, scope: str, variant: str) -> None:
    store = _seed(tmp_path, scope, "scope_deleted")
    payload = _payload(store, "scope_deleted")
    if variant.startswith("missing_"):
        payload.pop("scope" if variant == "missing_scope" else "scope_ref")
    elif variant == "unknown_scope":
        payload["scope"] = "other"
    elif variant == "scalar_scope":
        payload["scope"] = 1
    elif variant == "scalar_ref":
        payload["scope_ref"] = False
    else:
        assert variant == "bad_ref"
        payload["scope_ref"] = "nonempty" if scope == "user" else " "
    _change(store, "scope_deleted", payload)
    _assert_rejected(store, tmp_path)


@pytest.mark.parametrize("scope", ("user", "project", "session"))
@pytest.mark.parametrize("field", ("decayed_nodes", "decayed_edges"))
def test_partial_new_decay_pair_is_not_the_legacy_format(tmp_path: Path, scope: str, field: str) -> None:
    store = _seed(tmp_path, scope, "state_decayed")
    payload = _payload(store, "state_decayed")
    payload.pop(field)
    _change(store, "state_decayed", payload)
    _assert_rejected(store, tmp_path)


@pytest.mark.parametrize("scope", ("user", "project", "session"))
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("variant", ("valid", "boolean_count"))
@pytest.mark.parametrize("confirmed", (False, True))
def test_normal_cli_controls_confirmation_and_reports_payload_rejection(
    tmp_path: Path, scope: str, kind: str, variant: str, confirmed: bool,
) -> None:
    store = _seed(tmp_path, scope, kind)
    _replace_payload(store, kind, variant)
    before, history = store.graph.rows(), _disk_history(store)
    args = [sys.executable, "-m", "codey", "ghost", "rebuild-state", "--state-home", str(tmp_path)]
    if confirmed:
        args.append("--yes")
    completed = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert completed.returncode == (2 if not confirmed else 0 if variant == "valid" else 1), completed.stderr
    result: object = json.loads(completed.stdout)
    assert object_fields(result)
    if confirmed:
        assert result["ok"] is (variant == "valid")
    else:
        assert result["error"] == "rebuild-state requires --yes"
    assert store.graph.rows() == before and _disk_history(store) == history


@pytest.mark.parametrize("scope", ("user", "project", "session"))
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("action", ("delete_scope", "reset_all"))
def test_rejected_control_allows_disabled_explicit_deletion_and_cold_recovery(
    tmp_path: Path, scope: str, kind: str, action: str,
) -> None:
    store = _seed(tmp_path, scope, kind)
    _replace_payload(store, kind, "negative_count")
    assert not store.rebuild_from_events()
    history = _disk_history(store)
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert surface.inbox is not None
    for control in ("accept_events", "retrieve_projections", "run_maintenance", "admit_hints"):
        assert surface.inbox.set_control(control, False, scope="user")
    status, payload = surface.dispatch_action({
        "action": action, "scope": scope, "project": str(tmp_path / "project"), "session_id": "s1", "confirm": True,
    })
    assert status == 200 and payload["ok"] is True, payload
    reopened = GhostHebbianStore(tmp_path)
    assert not reopened.list_nodes() and not reopened.list_edges()
    assert reopened.rebuild_from_events() is (action == "reset_all")
    assert not reopened.list_nodes() and not reopened.list_edges()
    assert _disk_history(reopened)[:len(history)] == history


@pytest.mark.parametrize("scope", ("user", "project", "session"))
@pytest.mark.parametrize("kind", KINDS)
def test_valid_control_replay_rolls_back_projection_write_and_recovers_without_clock(
    tmp_path: Path, scope: str, kind: str,
) -> None:
    store = _seed(tmp_path, scope, kind)
    before, history = store.graph.rows(), _disk_history(store)

    def fail_after_write(nodes: Iterable[GhostNode], edges: Iterable[GhostEdge]) -> None:
        node_rows, edge_rows = tuple(nodes), tuple(edges)
        if kind != "scope_deleted":
            assert node_rows
        if kind == "state_decayed":
            assert edge_rows
        store.graph.replace_rows((), ())
        raise sqlite3.OperationalError("injected after control projection mutation")

    with patch.object(store, "_write_state", fail_after_write):
        assert not store.rebuild_from_events()
    assert store.graph.rows() == before and _disk_history(store) == history
    with patch("codey.ghost._common.now_iso_z", side_effect=AssertionError("replay read wall clock")):
        reopened = GhostHebbianStore(tmp_path)
        assert reopened.rebuild_from_events()
        assert reopened.rebuild_from_events()
    assert reopened.graph.rows() == before and _disk_history(reopened) == history
