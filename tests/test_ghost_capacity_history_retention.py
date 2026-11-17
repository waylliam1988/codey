"""Original audit history survives ordinary consumers at real capacity limits."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from codey.ghost.affinity_model import MAX_AFFINITY_EVENTS
from codey.ghost.continuity import MAX_CONTINUITY_EVENTS
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.deletion_manifest import DeletionManifest
from codey.ghost.graph_store import GhostGraph
from codey.ghost.hebbian import MAX_HEBBIAN_EVENTS
from codey.ghost.inbox import MAX_GHOST_EVENTS
from codey.ghost.observations import MAX_OBSERVATIONS
from codey.ghost.record_store import GhostRecords
from codey.ghost.sleep import MAX_SLEEP_EVENTS
from codey.ghost.work_queue_model import MAX_WORK_EVENTS
from tests.test_ghost_scope_history_retention import NOW, _history, _rebuild, _seed_history, _visible

EVENT_LIMITS: dict[str, int] = {
    "inbox": MAX_GHOST_EVENTS,
    "hebbian": MAX_HEBBIAN_EVENTS,
    "continuity": MAX_CONTINUITY_EVENTS,
    "work_queue": MAX_WORK_EVENTS,
    "affinity": MAX_AFFINITY_EVENTS,
    "sleep": MAX_SLEEP_EVENTS,
}


def _owner(surface: GhostControlSurface, store: str) -> GhostRecords | GhostGraph:
    assert surface.inbox is not None and surface.hebbian is not None and surface.continuity is not None
    assert surface.work_queue is not None and surface.affinity is not None and surface.sleep is not None
    owners: dict[str, GhostRecords | GhostGraph] = {
        "inbox": surface.inbox.records, "hebbian": surface.hebbian.graph,
        "continuity": surface.continuity.records, "work_queue": surface.work_queue.records,
        "affinity": surface.affinity.graph, "sleep": surface.sleep.records,
    }
    return owners[store]


def _event_size(event: dict[str, object]) -> int:
    return len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _repeated_events(event: dict[str, object], count: int) -> Iterator[dict[str, object]]:
    for index in range(count):
        yield dict(event, event_id=f"capacity-{index:06d}")


def _prepare_pressure(surface: GhostControlSurface, store: str, over_capacity: bool) -> None:
    owner = _owner(surface, store)
    original = owner.events()
    assert original
    event = max(original, key=_event_size)
    count = EVENT_LIMITS[store] + 1 if over_capacity else 2
    with owner.transaction():
        if isinstance(owner, GhostGraph):
            nodes, edges = owner.rows()
            owner.append(_repeated_events(event, count))
            owner.replace_rows(nodes, edges)
        else:
            owner.rows()
            owner.append(_repeated_events(event, count))
            owner.mark_applied()
    assert (owner.event_stats()["events"] > EVENT_LIMITS[store]) == over_capacity


@pytest.mark.parametrize("store", tuple(EVENT_LIMITS))
@pytest.mark.parametrize("scope", ["user", "project", "session"])
@pytest.mark.parametrize("over_capacity", [False, True])
@pytest.mark.parametrize("consumer", ["delete", "sleep"])
def test_ordinary_consumers_preserve_original_events_at_capacity(
    tmp_path: Path, store: str, scope: str, over_capacity: bool, consumer: str,
) -> None:
    surface = _seed_history(tmp_path, scope)
    _prepare_pressure(surface, store, over_capacity)
    before = _history(surface, store)
    assert before and _visible(surface, store), "invalid capacity fixture"
    if consumer == "delete":
        status, payload = surface.dispatch_action({
            "action": "delete_scope", "scope": scope, "project": str(tmp_path / "project"),
            "session_id": "session", "confirm": True, "delete_operation_id": "capacity-delete",
        })
        assert status == 200, payload
        manifest = DeletionManifest.from_payload(payload["deletion_manifest"])
        assert manifest.status == "completed" and manifest.retention_class == "tombstone"
    else:
        assert surface.sleep is not None
        report = surface.sleep.run_once(
            inbox_store=surface.inbox, hebbian_store=surface.hebbian, continuity_store=surface.continuity,
            work_queue_store=surface.work_queue, affinity_store=surface.affinity,
            run_id="capacity-maintenance", session_id="session", project=str(tmp_path / "project"),
        )
        assert not report.cancelled and not report.pending_steps
        assert all(step.ok for step in report.steps), report.warnings
    reopened = GhostControlSurface.from_state_home(tmp_path)
    _rebuild(reopened)
    assert _visible(reopened, store) == (consumer == "sleep"), "consumer/replay eligibility changed"
    preserved = _history(reopened, store)[:len(before)] == before
    assert preserved, "ordinary capacity maintenance rewrote original audit history"


@pytest.mark.parametrize("scope", ["user", "project", "session"])
@pytest.mark.parametrize("over_capacity", [False, True])
def test_observation_append_at_capacity_preserves_original_facts(
    tmp_path: Path, scope: str, over_capacity: bool,
) -> None:
    surface = _seed_history(tmp_path, scope)
    assert surface.observations is not None
    store = surface.observations
    count = MAX_OBSERVATIONS if over_capacity else 2
    with store.database.write_transaction() as database:
        database.executemany(
            "INSERT INTO observation_history(run_id,session_id,project,mode,user_text,assistant_text,"
            "stop_reason,committed,provider_id,ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                (f"capacity-{index:06d}", "session", str(tmp_path / "project"), "chat", "Capacity fact.",
                 "Recorded.", "done", 1, "fixture", NOW)
                for index in range(count)
            ),
        )
    before = _history(surface, "observations")
    assert (len(before) > MAX_OBSERVATIONS) == over_capacity
    assert _visible(surface, "observations")
    assert store.append_completed(
        run_id="capacity-new-run", session_id="session", project=str(tmp_path / "project"), mode="chat",
        user_text="Keep the new completion.", assistant_text="Recorded.", stop_reason="done",
    )
    reopened = GhostControlSurface.from_state_home(tmp_path)
    assert reopened.observations is not None
    assert any(row["run_id"] == "capacity-new-run" for row in reopened.observations.read_committed(scope="user"))
    preserved = _history(reopened, "observations")[:len(before)] == before
    assert preserved, "ordinary observation append physically trimmed original run facts"
