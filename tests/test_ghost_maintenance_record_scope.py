"""An allowed App trigger must preserve another scope's denied records."""
from __future__ import annotations

import hashlib
import json
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.ghost.affinity import GhostAffinityStore
from codey.ghost.affinity_events import _edge_reinforced_event, _node_reinforced_event, replay_affinity_events
from codey.ghost.affinity_model import AffinityEdgeSpec, AffinityNodeSpec, _node_id
from codey.ghost.affinity_sources import _scope_ref
from codey.ghost.continuity import GhostContinuityStore
from codey.ghost.graph_primitives import parse_ts
from codey.ghost.hebbian import GhostHebbianStore, GhostNode, _node_event
from codey.ghost.inbox import GhostInboxStore
from codey.ghost.sleep import GhostSleepStore
from codey.ghost.work_queue import GhostWorkQueueStore
from codey.ghost.work_queue_events import _observed_event
from codey.ghost.work_queue_model import GhostWorkItem
from tests.test_ghost_maintenance_revocation import _report

BASE = "2026-11-10T00:00:00Z"
LEASE = "2026-11-11T00:00:00Z"
DOMAINS = ("hebbian", "affinity", "continuity", "work_queue")


@dataclass(frozen=True)
class Scope:
    name: str
    raw_ref: str

    @property
    def project(self) -> str:
        return self.raw_ref if self.name == "project" else ""

    @property
    def session(self) -> str:
        return self.raw_ref if self.name == "session" else ""

    def stored_ref(self, domain: str) -> str:
        encoded = domain == "affinity" or (domain == "work_queue" and self.name == "session")
        return _scope_ref(self.name, self.raw_ref) if encoded else self.raw_ref


@dataclass(frozen=True)
class Stores:
    inbox: GhostInboxStore
    hebbian: GhostHebbianStore
    affinity: GhostAffinityStore
    continuity: GhostContinuityStore
    queue: GhostWorkQueueStore

    def snapshot(self, scope: Scope) -> dict[str, object]:
        nodes, edges = self.affinity.graph.rows()
        graph, _edges = self.hebbian.graph.rows()
        return {
            "hebbian": _scoped(graph, scope, "hebbian"),
            "affinity": (_scoped(nodes, scope, "affinity"), _scoped(edges, scope, "affinity")),
            "continuity": _scoped(self.continuity.records.rows(), scope, "continuity"),
            "work_queue": _scoped(self.queue.records.rows(), scope, "work_queue"),
        }


def _scoped(rows: list[dict[str, object]], scope: Scope, domain: str) -> list[dict[str, object]]:
    return [row for row in rows if row.get("scope") == scope.name and row.get("scope_ref") == scope.stored_ref(domain)]


def _seed(home: Path, target: Scope, peer: Scope) -> Stores:
    stores = Stores(GhostInboxStore(home), GhostHebbianStore(home), GhostAffinityStore(home),
                    GhostContinuityStore(home), GhostWorkQueueStore(home))
    hebbian_nodes: list[GhostNode] = []
    affinity_events: list[dict[str, object]] = []
    queue_items: list[GhostWorkItem] = []
    for label, scope in (("target", target), ("peer", peer)):
        hebbian_nodes.append(GhostNode(
            id=f"scope-node-{label}", kind="style_preference", label="Prefer concise replies.",
            conflict_key="reply_length", value_key="concise", status="active", scope=scope.name,
            scope_ref=scope.raw_ref, weight=0.8, confidence=0.9, candidate_ids=(), evidence_refs=(),
            created_at=BASE, updated_at=BASE, last_reinforced_at=BASE,
        ))
        ref = scope.stored_ref("affinity")
        for key in ("alpha", "beta"):
            affinity_events.append(_node_reinforced_event(AffinityNodeSpec(
                kind="research_concept", key=key, label=f"Concept {key}", scope=scope.name, scope_ref=ref,
                confidence=0.8, reward=1.0, source_refs=(f"research_interest:{label}:{key}",), evidence_refs=(),
            ), ts=BASE))
        affinity_events.append(_edge_reinforced_event(AffinityEdgeSpec(
            source=_node_id("research_concept", scope.name, ref, "alpha"),
            target=_node_id("research_concept", scope.name, ref, "beta"), relation="associated_with",
            scope=scope.name, scope_ref=ref, confidence=0.8, reward=1.0,
            source_refs=(f"research_interest:{label}:edge",), proof_refs=(),
        ), ts=BASE))
        for index in range(2):
            queue_items.append(GhostWorkItem(
                id=f"scope-work-{label}-{index}", kind="research", status="running", scope=scope.name,
                scope_ref=scope.stored_ref("work_queue"), title="Review maintenance scope boundaries",
                why_now="A persisted follow-up", priority=0.8, confidence=0.9, source="user",
                source_ref=f"scope-source-{label}-{index}", started_run_id=f"scope-run-{label}-{index}",
                retry_count=1, lease_expires_at=LEASE, created_at=BASE, updated_at=BASE,
            ))
    with stores.hebbian.graph.transaction():
        stores.hebbian.graph.append(_node_event(node, action="upsert", ts=BASE) for node in hebbian_nodes)
        stores.hebbian.graph.replace_rows((node.to_payload() for node in hebbian_nodes), ())
    nodes, edges = replay_affinity_events(affinity_events)
    with stores.affinity.graph.transaction():
        stores.affinity.graph.append(affinity_events)
        stores.affinity.graph.replace_rows((node.to_payload() for node in nodes), (edge.to_payload() for edge in edges))
    with stores.queue.records.transaction():
        stores.queue.records.append(_observed_event(item, ts=BASE) for item in queue_items)
        stores.queue.records.replace_rows(item.to_payload() for item in queue_items)
    assert len(stores.hebbian.list_nodes()) == 2
    stored_nodes, stored_edges = stores.affinity.graph.rows()
    assert len(stored_nodes) == 4 and len(stored_edges) == 2
    assert len(stores.queue.list_items()) == 4
    return stores


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.mark.parametrize("target_scope", ("project", "session"))
@pytest.mark.parametrize("trigger_scope", ("user", "project", "session"))
@pytest.mark.parametrize("revoked", (False, True))
def test_normal_app_preserves_explicitly_denied_record_scope(
    tmp_path: Path, target_scope: str, trigger_scope: str, revoked: bool,
) -> None:
    target = Scope(target_scope, str((tmp_path / "target-project").resolve()) if target_scope == "project" else "target-session")
    peer = Scope(target_scope, str((tmp_path / "peer-project").resolve()) if target_scope == "project" else "peer-session")
    trigger = Scope(trigger_scope, str((tmp_path / "peer-project").resolve()) if trigger_scope == "project"
                    else "peer-session" if trigger_scope == "session" else "")
    with closing(AppContext(tmp_path)) as app:
        stores = _seed(tmp_path, target, peer)
        app.ghost_inbox, app.ghost_hebbian = stores.inbox, stores.hebbian
        app.ghost_affinity, app.ghost_continuity, app.ghost_work_queue = stores.affinity, stores.continuity, stores.queue
        assert stores.inbox.set_control(
            "run_maintenance", not revoked, scope=target.name, project=target.project, session_id=target.session,
        )
        assert stores.inbox.control_enabled("run_maintenance", project=trigger.project, session_id=trigger.session)
        target_before, peer_before = stores.snapshot(target), stores.snapshot(peer)
        assert app.kick_ghost_sleep(project=trigger.project, session_id=trigger.session)
        assert app.wait_for_ghost_sleep(15)
        assert app.ghost_sleep_daemon.error_count == 0, app.ghost_sleep_daemon.last_error
        report = _report(GhostSleepStore(tmp_path))
        assert parse_ts(report.maintenance_as_of) > parse_ts(LEASE), "fixture must be due at the real cycle horizon"
        assert all(step.ok for step in report.steps) and not report.warnings, report.to_payload()
        assert not report.cancelled, "another scope's deny must not cancel the entire allowed cycle"
        target_after, peer_after = stores.snapshot(target), stores.snapshot(peer)
        target_changed = {domain: target_before[domain] != target_after[domain] for domain in DOMAINS}
        peer_changed = {domain: peer_before[domain] != peer_after[domain] for domain in DOMAINS}
        assert all(peer_changed.values()), "enabled peer must undergo actual maintenance in all four domains"
        target_queue = stores.queue.list_items(scope=target.name, project=target.project, session_id=target.session)
        peer_queue = stores.queue.list_items(scope=peer.name, project=peer.project, session_id=peer.session)
        assert len(target_queue) == len(peer_queue) == 2
        assert all(item.status == "queued" for item in peer_queue)
        expected_target_status = "running" if revoked else "queued"
        passed = all(changed != revoked for changed in target_changed.values()) and all(
            item.status == expected_target_status for item in target_queue
        )
        measurement: dict[str, object] = {
            "target_scope": target_scope, "trigger_scope": trigger_scope, "revoked": revoked, "fixture_valid": True,
            "target_before_digest": _digest(target_before), "target_after_digest": _digest(target_after),
            "peer_before_digest": _digest(peer_before), "peer_after_digest": _digest(peer_after),
            "target_changed": target_changed, "peer_changed": peer_changed,
            "target_queue_statuses": [item.status for item in target_queue],
            "peer_queue_statuses": [item.status for item in peer_queue], "pass": passed,
        }
        print("MAINTENANCE_RECORD_SCOPE_CELL " + json.dumps(measurement, sort_keys=True), flush=True)
        assert passed, measurement
