"""Journal replay, controls and explicit correction through production memory ports."""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.app.memory_routes import memory_action, memory_summary
from codey.ghost.backend import GhostMemory
from codey.memory.context import retrieve_context
from codey.memory.contracts import (
    MEMORY_CONTROLS,
    DeleteScopeRequest,
    MemoryCapabilities,
    MemoryChange,
    MemoryControl,
    MemoryDecision,
    MemoryPolicy,
    MemoryQuery,
)
from codey.memory.service import MemoryService
from tests.test_memory_boundary import NOW, SCOPE, backend, event

LATER = "2026-11-21T00:00:00+00:00"
EXPIRED = "2026-11-22T00:00:00+00:00"


def confirm(store: GhostMemory, identity: str, *, text: str = "Use Python", slot: str = "",
            context: str = "", expires_at: str = "", supersedes: tuple[str, ...] = (),
            scope: str = SCOPE, controls: tuple[str, ...] = ()) -> str:
    store.observe(replace(event(identity), kind="memory.proposed", run_id="", scope=scope,
                          control_scopes=controls, payload={"kind": "preference", "text": text,
                          "slot": slot, "context": context, "expires_at": expires_at, "supersedes": list(supersedes)}))
    proposal = next(row for row in store.proposals(scope) if row.proposal_id == identity)
    store.decide(MemoryDecision(identity, proposal.version, True, "decision:" + identity, NOW))
    return identity


def memory(store: GhostMemory) -> MemoryService:
    return MemoryService(MemoryCapabilities(store, store, store, store, store, store, store, store),
                         source_available=lambda _ref: True, clock=lambda: NOW)


def test_full_journal_rebuilds_corrections_deletion_and_controls(tmp_path: Path) -> None:
    store = backend(tmp_path)
    confirm(store, "first", slot="language")
    confirm(store, "second", text="Use Chinese", slot="language")
    confirm(store, "other", scope="project:other")
    store.set_control(MemoryControl(SCOPE, "run_maintenance", False, "control:off", NOW))
    store.change(MemoryChange("second", SCOPE, "quarantined", "change:q", LATER))
    store.delete_scope(DeleteScopeRequest("project:other", requested_at=LATER))
    before = store.export_state()
    first = store.rebuild()
    assert first.events == 9
    assert store.export_state() == before
    assert store.rebuild() == first
    reopened = backend(tmp_path)
    assert reopened.rebuild() == first
    assert reopened.export_state() == before


@pytest.mark.parametrize("name", MEMORY_CONTROLS)
def test_controls_are_independent_durable_and_cannot_override_parent_deny(tmp_path: Path, name: str) -> None:
    store = backend(tmp_path)
    control = next(value for value in MEMORY_CONTROLS if value == name)
    store.set_control(MemoryControl("user", control, False, "control:parent:" + name, NOW))
    store.set_control(MemoryControl(SCOPE, control, True, "control:child:" + name, NOW))
    actual = store.policy(("user", SCOPE)).to_payload()
    assert actual == {value: value != name for value in MEMORY_CONTROLS}
    assert backend(tmp_path).policy((SCOPE,)).to_payload() == actual
    store.rebuild()
    assert store.policy((SCOPE,)).to_payload() == actual


def test_disabled_ingestion_does_not_replay_as_new_learning_after_enable(tmp_path: Path) -> None:
    store = backend(tmp_path)
    store.set_control(MemoryControl(SCOPE, "accept_events", False, "control:off", NOW))
    store.observe(event())
    store.set_control(MemoryControl(SCOPE, "accept_events", True, "control:on", NOW))
    store.observe(event())
    assert store.retrieve(MemoryQuery("Python", (SCOPE,), NOW)).items == ()
    store.rebuild()
    assert store.retrieve(MemoryQuery("Python", (SCOPE,), NOW)).items == ()
    store.observe(event("new"))
    assert len(store.retrieve(MemoryQuery("Python", (SCOPE,), NOW)).items) == 1


def test_retrieval_and_admission_switches_are_distinct_and_rechecked(tmp_path: Path) -> None:
    store = backend(tmp_path)
    confirm(store, "preference")
    service = memory(store)
    frozen = retrieve_context(service, task="Python", project="", session_id="s")
    assert frozen.items and frozen.valid(service)
    store.set_control(MemoryControl(SCOPE, "admit_hints", False, "control:hints", NOW))
    assert service.retrieve(frozen.query).items
    assert not frozen.valid(service)
    omitted = retrieve_context(service, task="Python", project="", session_id="s")
    assert omitted.items == () and any(row.reason == "hints_disabled" for row in omitted.traces)
    store.set_control(MemoryControl(SCOPE, "retrieve_projections", False, "control:read", NOW))
    assert not service.retrieve(frozen.query).items
    assert store.export_state()["items"]


def test_maintenance_respects_each_records_origin_scope(tmp_path: Path) -> None:
    store = backend(tmp_path)
    confirm(store, "paused", expires_at=LATER, controls=("user", SCOPE))
    confirm(store, "allowed", scope="project:other", expires_at=LATER, controls=("user", "project:other"))
    store.set_control(MemoryControl(SCOPE, "run_maintenance", False, "control:pause", NOW))
    store.maintain(as_of=EXPIRED)
    records = {row.item.item_id: row.state for row in store.records((SCOPE, "project:other"))}
    assert records == {"paused": "active", "allowed": "expired"}
    assert not store.retrieve(MemoryQuery("Python", (SCOPE,), EXPIRED)).items
    expected = store.export_state()
    store.rebuild()
    assert store.export_state() == expected


def test_conflicts_are_scoped_and_conditioned_and_explicitly_superseded(tmp_path: Path) -> None:
    store = backend(tmp_path)
    confirm(store, "chat", slot="language", context="chat")
    confirm(store, "research", slot="language", context="research")
    confirm(store, "chat-new", text="Use Chinese", slot="language", context="chat")
    states = {row.item.item_id: row.state for row in store.records((SCOPE,))}
    assert states == {"chat": "superseded", "research": "active", "chat-new": "active"}
    picked = store.retrieve(MemoryQuery("", (SCOPE,), NOW, context="chat"))
    assert [item.item_id for item in picked.items] == ["chat-new"]
    assert {trace.reason for trace in picked.traces} >= {"superseded", "context_mismatch"}
    confirm(store, "replacement", supersedes=("chat-new",))
    assert next(row for row in store.records((SCOPE,)) if row.item.item_id == "chat-new").superseded_by == "replacement"
    with pytest.raises(ValueError, match="superseded"):
        store.change(MemoryChange("chat", SCOPE, "active", "restore:old", NOW))


def test_bad_correction_is_atomic_and_keeps_proposal_reviewable(tmp_path: Path) -> None:
    store = backend(tmp_path)
    confirm(store, "other", scope="project:other")
    store.observe(replace(event("bad"), kind="memory.proposed", payload={
        "kind": "preference", "text": "Wrong scope", "supersedes": ["other"],
    }))
    proposal, = store.proposals(SCOPE)
    with pytest.raises(ValueError, match="same kind and scope"):
        store.decide(MemoryDecision("bad", proposal.version, True, "decision:bad", NOW))
    assert store.proposals(SCOPE) == (proposal,)
    assert store.records(("project:other",))[0].state == "active"


@pytest.mark.parametrize("damage", ["payload", "tail", "head"])
def test_corrupt_replay_never_replaces_live_projections(tmp_path: Path, damage: str) -> None:
    store = backend(tmp_path)
    confirm(store, "keep")
    before = store.export_state()
    with store.database.connect(write=True) as connection:
        if damage == "payload":
            connection.execute("UPDATE events SET event_json = '{}' WHERE sequence = 1")
        elif damage == "tail":
            connection.execute("DELETE FROM events WHERE sequence = 2")
        else:
            connection.execute("UPDATE journal_head SET payload = '{}' WHERE id = 1")
    with pytest.raises(ValueError):
        store.rebuild()
    assert store.export_state() == before


def test_event_identity_cannot_name_different_contents(tmp_path: Path) -> None:
    store = backend(tmp_path)
    store.observe(event())
    with pytest.raises(ValueError, match="different content"):
        store.observe(replace(event(), payload={"user_text": "changed", "assistant_text": "changed"}))
    assert len(store.retrieve(MemoryQuery("Python", (SCOPE,), NOW)).items) == 1


def test_normal_management_user_scope_retry_and_history_isolation(tmp_path: Path) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        state.durable.create_conversation("s")
        body: dict[str, object] = {"action": "propose", "scope_kind": "user", "session_id": "s",
                                   "kind": "preference", "text": "Use Python", "request_id": "one-proposal"}
        first = memory_action(state, body)
        assert first[0] == 200 and memory_action(state, body) == first
        proposal, = state.memory.proposals("user")
        assert memory_action(state, {**body, "action": "accept", "proposal_id": proposal.proposal_id,
                                     "version": proposal.version})[0] == 200
        assert memory_action(state, {"action": "control", "session_id": "s", "scope_kind": "user",
                                     "control": "admit_hints", "enabled": False})[0] == 200
        assert not retrieve_context(state.memory, task="Python", project="D:/another", session_id="s").items
        assert state.durable.context_snapshot("s", token_budget=4096).messages == ()
        assert memory_action(state, {"action": "rebuild", "session_id": "s"})[0] == 200
        assert not state.memory.policy(("user",)).admit_hints


def test_explain_reports_whole_item_budget_and_source_rejection(tmp_path: Path) -> None:
    store = backend(tmp_path)
    confirm(store, "first")
    service = memory(store)
    view = retrieve_context(service, task="Python", project="", session_id="s", budget=5)
    assert not view.items and any(row.reason == "over_budget" for row in view.traces)
    assert store.policy((SCOPE,)) == MemoryPolicy()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.1, True])
def test_invalid_confidence_never_enters_the_journal(tmp_path: Path, value: object) -> None:
    from codey.types.json_value import decode_json_object

    store = backend(tmp_path)
    with pytest.raises(ValueError):
        payload = decode_json_object({"kind": "hypothesis", "text": "Python", "confidence": value})
        store.observe(replace(event("bad"), kind="memory.proposed", payload=payload))
    assert store.rebuild().events == 0


def test_scope_deletion_watermark_never_moves_backwards(tmp_path: Path) -> None:
    store = backend(tmp_path)
    store.delete_scope(DeleteScopeRequest(SCOPE, requested_at=LATER))
    store.delete_scope(DeleteScopeRequest(SCOPE, requested_at=NOW))
    store.observe(event("delayed"))
    assert store.records((SCOPE,)) == ()
    store.observe(replace(event("new"), occurred_at=EXPIRED))
    expected = store.export_state()
    assert [row.item.item_id for row in store.records((SCOPE,))] == ["new"]
    store.rebuild()
    assert store.export_state() == expected


def test_canonical_proposal_and_reset_retries_preserve_original_time(tmp_path: Path) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        state.durable.create_conversation("s")
        state.memory.clock = lambda: NOW
        body: dict[str, object] = {"action": "propose", "session_id": "s", "kind": "preference",
            "text": "Use Python", "expires_at": LATER, "request_id": "expiring-proposal"}
        first = memory_action(state, body)
        assert first[0] == 200
        state.memory.clock = lambda: EXPIRED
        assert memory_action(state, body) == first
        reset: dict[str, object] = {"action": "reset", "session_id": "s", "request_id": "one-reset"}
        original = memory_action(state, reset)
        assert original == (200, {"ok": True, "deleted": 0})
        count = state.memory.rebuild().events
        assert memory_action(state, reset) == original
        assert state.memory.rebuild().events == count


def test_summary_of_parent_controls_excludes_child_denials(tmp_path: Path) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        state.durable.create_conversation("s")
        assert memory_action(state, {"action": "control", "session_id": "s", "control": "admit_hints",
                                     "enabled": False})[0] == 200
        status, parent = memory_summary(state, {"session_id": ["s"], "scope_kind": ["user"]})
        assert status == 200 and parent["controls"] == MemoryPolicy().to_payload()
        status, child = memory_summary(state, {"session_id": ["s"]})
        assert status == 200 and child["controls"] == MemoryPolicy(admit_hints=False).to_payload()
