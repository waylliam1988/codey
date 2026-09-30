"""Work-queue events own the pure items<-events projection.

Locks: observed events replay into work items with list semantics;
claim/complete transitions validate and apply; snapshots round-trip;
the events leaf never imports the Store module.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _item(status="queued"):
    from codey.ghost.work_queue_model import GhostWorkItem

    return GhostWorkItem(
        id="w1", kind="coding", status=status, scope="project", scope_ref="proj",
        title="t", why_now="why", priority=0.5, confidence=0.5,
        source="continuity", source_ref="src",
    )


def test_observed_event_replays_into_one_item():
    from codey.ghost.work_queue_events import items_from_events

    event = {
        "schema_version": 1, "type": "ghost_work_item_observed",
        "event_id": "e1", "ts": "2026-10-01T00:00:00Z", "item": _item().to_payload(),
    }
    restored = items_from_events([event])
    assert isinstance(restored, list)
    assert len(restored) == 1
    assert restored[0].id == "w1"
    assert restored[0].status == "queued"


def test_snapshot_and_incremental_replay_agree():
    from codey.ghost.work_queue_events import items_from_events

    item = _item()
    snapshot = {
        "schema_version": 1, "type": "ghost_work_snapshot",
        "event_id": "s1", "ts": "2026-10-01T00:00:00Z", "reason": "test",
        "items": [item.to_payload()],
    }
    observed = {
        "schema_version": 1, "type": "ghost_work_item_observed",
        "event_id": "e1", "ts": "2026-10-01T00:00:00Z", "item": item.to_payload(),
    }
    assert [row.id for row in items_from_events([snapshot])] == ["w1"]
    assert [row.id for row in items_from_events([observed])] == ["w1"]


def test_claim_and_complete_transitions_apply():
    from codey.ghost.work_queue_events import _apply_transition_event, _valid_work_transition

    item = _item(status="queued")
    by_id = {item.id: item}
    claim = {
        "item_id": item.id, "action": "claim",
        "precondition": {
            "expected_status": "queued", "expected_started_run_id": "",
            "expected_retry_count": 0,
        },
        "patch": {
            "status": "running", "started_run_id": "run-1", "retry_count": 1,
            "lease_expires_at": "2026-06-01T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z",
        },
    }
    assert _valid_work_transition(claim) is True
    assert _apply_transition_event(by_id, claim, now="2026-10-01T00:00:00Z") == "applied"
    assert by_id[item.id].status == "running"


def test_stale_lease_cannot_complete_new_claim():
    from dataclasses import replace

    from codey.ghost.work_queue_events import _apply_transition_event

    item = replace(_item(status="running"), started_run_id="run-old", retry_count=1)
    by_id = {item.id: item}
    complete_with_wrong_run = {
        "item_id": item.id, "action": "complete",
        "precondition": {
            "expected_status": "running", "expected_started_run_id": "run-old",
            "expected_retry_count": 1,
        },
        "patch": {
            "status": "done", "completed_run_id": "run-new",
            "proof_refs": ["diff:run-new"], "updated_at": "2026-10-01T00:00:00Z",
        },
    }
    assert _apply_transition_event(by_id, complete_with_wrong_run, now="2026-10-01T00:00:00Z") == "invalid"
    assert by_id[item.id].status == "running"


def test_events_leaf_does_not_import_store():
    import ast

    path = ROOT / "codey" / "ghost" / "work_queue_events.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not any(n.name == "codey.ghost.work_queue" for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module != "codey.ghost.work_queue"
            assert node.module != "codey.ghost.work_queue_sources"
