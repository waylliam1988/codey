"""Hebbian record timestamps must be validated before replacing a good projection."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.ghost.directive import GhostDirective, build_ghost_directive
from codey.ghost.hebbian import GhostHebbianStore
from codey.ghost.payload_types import object_fields
from tests.test_ghost_hebbian_replay_integrity import NOW, _seed

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
