"""Hebbian record timestamps must be validated before replacing a good projection."""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.app.context import AppContext
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.directive import GhostDirective, build_ghost_directive
from codey.ghost.hebbian import GhostEdge, GhostHebbianStore, GhostNode
from codey.ghost.payload_types import object_fields
from codey.ghost.sleep import GhostSleepStore
from tests.test_ghost_hebbian_replay_integrity import NOW, _seed
from tests.test_ghost_language_production import (
    LANGUAGE_LINE,
    Capture,
    _execute,
    _provider,
    _remember,
    _state_home,
)
from tests.test_ghost_language_production import capture_endpoint as capture_endpoint
from tests.test_ghost_scope_history_retention import _history

VARIANTS = (
    "valid", "created_at", "updated_at", "last_reinforced_at", "last_decayed_at",
    "nonstring", "missing_anchor", "legacy_optional", "legacy_fallback",
)
VALID = ("valid", "legacy_optional", "legacy_fallback")


def _encode(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _inject(store: GhostHebbianStore, target: str, variant: str) -> None:
    if variant == "valid":
        return
    kind = "ghost_hebbian_node_upsert" if target == "node" else "ghost_hebbian_edge_upsert"
    event = [row for row in store.graph.events() if row["type"] == kind][-1]
    row = event[target]
    assert object_fields(row)
    altered = dict(row)
    if variant == "nonstring":
        altered["last_decayed_at"] = False
    elif variant == "missing_anchor":
        for field in ("updated_at", "last_reinforced_at", "last_decayed_at"):
            altered[field] = ""
    elif variant == "legacy_optional":
        altered["created_at"] = altered["last_decayed_at"] = ""
    elif variant == "legacy_fallback":
        altered["updated_at"] = altered["last_decayed_at"] = ""
    else:
        assert variant in ("created_at", "updated_at", "last_reinforced_at", "last_decayed_at")
        altered[variant] = "not-a-date"
    changed = dict(event)
    changed[target] = altered
    with store.graph.transaction():
        # Disk-corruption injection retains the event identity, sequence,
        # cursor and last valid projection; the normal producer is unchanged.
        updated = store.graph._db().execute(
            "UPDATE graph_events SET payload=? WHERE graph='hebbian' AND payload=?",
            (_encode(changed), _encode(event)),
        )
        assert updated.rowcount == 1
        if variant in VALID:
            nodes, edges = store.graph.rows()
            rows = nodes if target == "node" else edges
            keys = ("id",) if target == "node" else ("source", "target", "relation")
            for index, current in enumerate(rows):
                if all(current[key] == row[key] for key in keys):
                    rows[index] = altered
                    break
            else:
                raise AssertionError("legacy fixture did not match its committed projection")
            store.graph.replace_rows(nodes, edges)


def _directive(store: GhostHebbianStore, home: Path) -> tuple[GhostDirective | None, str]:
    try:
        return build_ghost_directive(store, project=str(home / "project"), session_id="s1"), ""
    except (ValueError, TypeError) as error:
        return None, type(error).__name__


@pytest.mark.parametrize("scope", ("user", "project", "session"))
@pytest.mark.parametrize("target", ("node", "edge"))
@pytest.mark.parametrize("variant", VARIANTS)
def test_internal_record_time_cannot_pollute_rebuild_or_break_normal_directive(
    tmp_path: Path, scope: str, target: str, variant: str,
) -> None:
    with patch("codey.ghost._common.now_iso_z", return_value=NOW):
        store = _seed(tmp_path, scope)
        assert store.graph.rows()[0] and store.graph.rows()[1]
        _inject(store, target, variant)
        before, events = store.graph.rows(), store.graph.events()
        directive, error = _directive(store, tmp_path)
        assert directive is not None and directive.text and not error, "invalid fixture"
        accepted: list[bool] = []
        unchanged: list[bool] = []
        consumer_errors: list[str] = []
        directive_unchanged: list[bool] = []
        for restart in (False, True):
            if restart:
                store = GhostHebbianStore(tmp_path)
            accepted.append(store.rebuild_from_events())
            unchanged.append(store.graph.rows() == before)
            current, consumer_error = _directive(store, tmp_path)
            consumer_errors.append(consumer_error)
            directive_unchanged.append(current == directive)
            assert store.graph.events() == events
        measurement: dict[str, object] = {
            "scope": scope, "target": target, "variant": variant, "accepted": accepted,
            "projection_unchanged": unchanged, "directive_unchanged": directive_unchanged,
            "consumer_errors": consumer_errors,
        }
        print("HEBBIAN_ROW_TIME_CELL " + json.dumps(measurement, sort_keys=True))
        assert accepted == [variant in VALID] * 2
        assert all(unchanged), "replay replaced good state with invalid historical time"
        assert consumer_errors == ["", ""] and all(directive_unchanged)


def _payload(target: str) -> dict[str, object]:
    if target == "node":
        return GhostNode(
            id="row-time-node", kind="style_preference", label="Concise answers.",
            conflict_key="style_preference:reply_length", value_key="concise", status="active",
            scope="user", scope_ref="", weight=0.8, confidence=1.0, candidate_ids=("candidate",),
            evidence_refs=("candidate:1",), created_at=NOW, updated_at=NOW,
            last_reinforced_at=NOW, last_decayed_at=NOW,
        ).to_payload()
    assert target == "edge"
    return GhostEdge(
        source="first", target="second", relation="coactivated_with", weight=0.8,
        candidate_ids=("candidate",), evidence_refs=("candidate:1",), created_at=NOW,
        updated_at=NOW, last_reinforced_at=NOW, last_decayed_at=NOW,
    ).to_payload()


def _decode(target: str, payload: object) -> GhostNode | GhostEdge | None:
    return GhostNode.from_payload(payload) if target == "node" else GhostEdge.from_payload(payload)


@pytest.mark.parametrize("target", ("node", "edge"))
@pytest.mark.parametrize("field", ("created_at", "updated_at", "last_reinforced_at", "last_decayed_at"))
@pytest.mark.parametrize("bad", (None, False, 7, 1.5, " ", "not-a-date", "2026-02-30T00:00:00Z",
                                 "2026-01-01T00:00:00+25:00"))
def test_decoder_rejects_raw_bad_times_before_string_coercion(target: str, field: str, bad: object) -> None:
    payload = _payload(target)
    payload[field] = bad
    assert _decode(target, payload) is None


@pytest.mark.parametrize("target", ("node", "edge"))
@pytest.mark.parametrize("timestamp", (NOW, "2026-11-17T00:00:00", "2026-11-17T08:00:00+08:00"))
def test_decoder_preserves_valid_legacy_timezone_forms_without_reading_clock(target: str, timestamp: str) -> None:
    payload = _payload(target)
    for field in ("created_at", "updated_at", "last_reinforced_at", "last_decayed_at"):
        payload[field] = timestamp
    with patch("codey.ghost._common.now_iso_z", side_effect=AssertionError("decoder consulted clock")):
        decoded = _decode(target, payload)
    assert decoded is not None and decoded.updated_at == timestamp


def _corrupt_projection(store: GhostHebbianStore, target: str) -> None:
    with store.graph.transaction():
        nodes, edges = store.graph.rows()
        rows = nodes if target == "node" else edges
        assert rows
        # Deliberate projection corruption; the authoritative log stays valid.
        rows[0] = {**rows[0], "last_decayed_at": "not-a-date"}
        store.graph.replace_rows(nodes, edges)


@pytest.mark.parametrize("scope", ("user", "project", "session"))
@pytest.mark.parametrize("target", ("node", "edge"))
@pytest.mark.parametrize("recovery", ("rebuild_delete", "reset"))
def test_bad_projection_fails_closed_in_reader_sleep_and_recovers_or_resets(
    tmp_path: Path, scope: str, target: str, recovery: str,
) -> None:
    with patch("codey.ghost._common.now_iso_z", return_value=NOW):
        store = _seed(tmp_path, scope)
        before, events = store.graph.rows(), store.graph.events()
        _corrupt_projection(store, target)
        corrupt = store.graph.rows()
        with pytest.raises(OSError, match="invalid Ghost Hebbian graph row"):
            store.list_nodes()
        directive, error = _directive(store, tmp_path)
        assert directive is not None and not directive.text and not error
        assert directive.warnings == ("store_unreadable",)
        report = GhostSleepStore(tmp_path).run_once(hebbian_store=store, maintenance_as_of=NOW)
        steps = {step.name: step for step in report.steps}
        assert not steps["hebbian_decay"].ok and steps["hebbian_decay"].skipped_reason == "step_error"
        assert "hebbian_decay:OSError" in report.warnings
        assert store.graph.rows() == corrupt and store.graph.events() == events
        surface = GhostControlSurface.from_state_home(tmp_path)
        history = _history(surface, "hebbian")
        assert surface.inbox is not None
        for control in ("accept_events", "retrieve_projections", "run_maintenance", "admit_hints"):
            assert surface.inbox.set_control(control, False, scope="user")
        if recovery == "rebuild_delete":
            assert GhostHebbianStore(tmp_path).rebuild_from_events()
            assert store.graph.rows() == before
        status, payload = surface.dispatch_action({
            "action": "delete_scope" if recovery == "rebuild_delete" else "reset_all",
            "scope": scope, "project": str(tmp_path / "project"), "session_id": "s1", "confirm": True,
        })
        assert status == 200 and payload["ok"] is True, payload
        reopened = GhostHebbianStore(tmp_path)
        assert reopened.rebuild_from_events()
        assert not reopened.list_nodes() and not reopened.list_edges()
        assert _history(surface, "hebbian")[:len(history)] == history


@pytest.mark.parametrize("mode", ("corrupt", "recovered", "retrieve_off", "admit_off"))
def test_normal_app_http_omits_invalid_projection_and_can_resume_reviewed_language(
    tmp_path: Path, capture_endpoint: Capture, mode: str,
) -> None:
    endpoint, payloads = capture_endpoint
    with closing(AppContext(_state_home(tmp_path))) as state:
        surface, _candidate_id = _remember(state)
        assert surface.hebbian is not None and surface.inbox is not None
        _corrupt_projection(surface.hebbian, "node")
        if mode == "recovered":
            assert surface.hebbian.rebuild_from_events()
        if mode in {"retrieve_off", "admit_off"}:
            control = "retrieve_projections" if mode == "retrieve_off" else "admit_hints"
            assert surface.inbox.set_control(control, False, scope="session", session_id="chat-workspace")
        event = _execute(state, _provider(endpoint), task="Explain eventual consistency.")
    assert event["stop_reason"] == "done", event
    assert len(payloads) == 1
    assert (LANGUAGE_LINE in json.dumps(payloads[0], ensure_ascii=False)) == (mode == "recovered")
