"""One Sleep cycle must use its entry horizon despite later wall-clock jumps."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.ghost import _common
from codey.ghost.affinity import GhostAffinityStore
from codey.ghost.affinity_events import replay_affinity_events
from codey.ghost.continuity import GhostContinuityStore
from codey.ghost.hebbian import GhostHebbianStore, GhostNode, _node_event
from codey.ghost.payload_types import object_fields
from codey.ghost.sleep import GhostSleepStore
from codey.ghost.work_queue import GhostWorkQueueStore
from codey.ghost.work_queue_events import _observed_event, items_from_events
from codey.ghost.work_queue_model import GhostWorkItem
from tests.test_ghost_replay_clock_boundary import BASE, _events

HORIZON = "2026-04-01T00:00:00Z"
FORWARD = "2026-07-01T00:00:00Z"
BACKWARD = "2026-01-02T00:00:00Z"
DOMAINS = ("hebbian_decay", "affinity_decay", "continuity_refresh", "queue_reconcile", "affinity_sync")


@dataclass
class CycleClock:
    after_entry: str
    calls: int = 0

    def __call__(self) -> str:
        self.calls += 1
        return HORIZON if self.calls == 1 else self.after_entry


def _fixed_event_ids(events: list[dict[str, object]]) -> list[dict[str, object]]:
    return [{**event, "event_id": f"clock-seed-{index}"} for index, event in enumerate(events)]


def _seed_hebbian(home: Path, scope: str, scope_ref: str) -> GhostHebbianStore:
    node = GhostNode(
        id="clock-node", kind="style_preference", label="Prefer concise replies.",
        conflict_key="reply_length", value_key="concise", status="active", scope=scope,
        scope_ref=scope_ref, weight=0.8, confidence=0.9, candidate_ids=(), evidence_refs=(),
        created_at=BASE, updated_at=BASE, last_reinforced_at=BASE,
    )
    store = GhostHebbianStore(home)
    with patch.object(_common, "now_iso_z", return_value=BASE):
        events = _fixed_event_ids([_node_event(node, action="upsert")])
    with store.graph.transaction():
        store.graph.append(events)
        store.graph.replace_rows([node.to_payload()], ())
    assert store.list_nodes(scope=scope, project=scope_ref, session_id=scope_ref) == (node,)
    return store


def _seed_queue(home: Path, scope: str, scope_ref: str, *, source_only: bool) -> GhostWorkQueueStore:
    items = [GhostWorkItem(
        id=f"clock-work-{index}", kind="research", status="queued" if source_only else "running",
        scope=scope, scope_ref=scope_ref, title="Review recovery behavior", why_now="A saved follow-up",
        priority=0.8, confidence=0.9, source="user", source_ref=f"clock-source-{index}",
        started_run_id="" if source_only else f"run-{index}", retry_count=1,
        lease_expires_at="" if source_only else lease, created_at=BASE, updated_at=BASE,
        expires_at="2026-04-02T00:00:00Z" if source_only else "",
    ) for index, lease in enumerate(("2026-03-01T00:00:00Z", "2026-04-02T00:00:00Z"))]
    events = _fixed_event_ids([_observed_event(item, ts=BASE) for item in items])
    assert items_from_events(events) == items
    store = GhostWorkQueueStore(home)
    with store.records.transaction():
        store.records.append(events)
        store.records.replace_rows(item.to_payload() for item in items)
    assert store.records.rows() == [item.to_payload() for item in items]
    return store


def _cycle(home: Path, scope: str, scope_ref: str, domain: str, clock: CycleClock) -> dict[str, object]:
    hebbian: GhostHebbianStore | None = None
    affinity: GhostAffinityStore | None = None
    continuity: GhostContinuityStore | None = None
    queue: GhostWorkQueueStore | None = None
    if domain in {"hebbian_decay", "continuity_refresh"}:
        hebbian = _seed_hebbian(home, scope, scope_ref)
        if domain == "continuity_refresh":
            continuity = GhostContinuityStore(home)
    elif domain == "affinity_decay":
        initial, _suffix = _events(scope, scope_ref, "valid")
        initial = _fixed_event_ids(initial)
        nodes, edges = replay_affinity_events(initial)
        affinity = GhostAffinityStore(home)
        with affinity.graph.transaction():
            affinity.graph.append(initial)
            affinity.graph.replace_rows((node.to_payload() for node in nodes), (edge.to_payload() for edge in edges))
        assert len(affinity.list_nodes(scope=scope, project=scope_ref, session_id=scope_ref)) == 2
    else:
        queue = _seed_queue(home, scope, scope_ref, source_only=domain == "affinity_sync")
        if domain == "affinity_sync":
            affinity = GhostAffinityStore(home)
    sleep = GhostSleepStore(home)
    with patch.object(_common, "now_iso_z", clock):
        report = sleep.run_once(
            hebbian_store=hebbian, affinity_store=affinity, continuity_store=continuity,
            work_queue_store=queue, session_id=scope_ref if scope == "session" else "",
            project=scope_ref if scope == "project" else "",
        )
    assert report.started_at == HORIZON
    assert not report.cancelled and not report.warnings
    assert all(step.ok for step in report.steps)
    step_name = "event_compaction" if domain == "queue_reconcile" else domain
    step = next(step for step in report.steps if step.name == step_name)
    rows: object
    if domain == "hebbian_decay":
        rows = GhostHebbianStore(home).graph.rows()
    elif domain in {"affinity_decay", "affinity_sync"}:
        rows = GhostAffinityStore(home).graph.rows()
    elif domain == "continuity_refresh":
        rows = GhostContinuityStore(home).records.rows()
    else:
        rows = GhostWorkQueueStore(home).records.rows()
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"rows": rows, "digest": digest, "counts": dict(step.counts), "skipped_reason": step.skipped_reason}


@pytest.mark.parametrize("scope", ("user", "project", "session"))
@pytest.mark.parametrize("domain", DOMAINS)
def test_sleep_decisions_use_one_entry_horizon(tmp_path: Path, scope: str, domain: str) -> None:
    scope_ref = str((tmp_path / "project").resolve()) if scope == "project" else "s1" if scope == "session" else ""
    results = {profile: _cycle(tmp_path / profile, scope, scope_ref, domain, CycleClock(instant))
               for profile, instant in (("fixed", HORIZON), ("repeat", HORIZON), ("forward", FORWARD), ("backward", BACKWARD))}
    fixed = results["fixed"]
    assert fixed == results["repeat"], "constant-clock repeat must be deterministic"
    counts = fixed["counts"]
    assert object_fields(counts)
    control = "stale_work_claims" if domain == "queue_reconcile" else "items_changed" if domain == "continuity_refresh" else "nodes_changed" if domain == "affinity_sync" else "decayed_nodes"
    changed: object = counts[control]
    assert isinstance(changed, int) and changed > 0, "fixed-clock control must perform a real transition"
    measurement: dict[str, object] = {"scope": scope, "domain": domain, "results": results}
    print("SLEEP_CLOCK_CELL " + json.dumps(measurement, sort_keys=True))
    assert fixed == results["forward"], "forward jump changed a cycle with the same entry horizon"
    assert fixed == results["backward"], "backward jump changed a cycle with the same entry horizon"


def test_cycle_clock_fixture_keeps_entry_horizon_and_changes_only_later_reads() -> None:
    clock = CycleClock(FORWARD)
    assert [clock(), clock(), clock()] == [HORIZON, FORWARD, FORWARD]
