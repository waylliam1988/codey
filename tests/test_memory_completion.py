"""Production candidate transport and the versioned memory learning lifecycle."""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from urllib.request import Request

import pytest

from codey.app.context import AppContext
from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.app.memory_routes import memory_action
from codey.memory.codec import digest
from codey.memory.contracts import (
    MemoryEvaluation,
    MemoryEvent,
    MemoryFamilyChange,
    MemoryMaintenanceRequest,
    MemoryQuery,
    MemoryRetentionPolicy,
)
from codey.providers import api_transport
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ChatProvider
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from tests.test_memory_boundary import NOW, SCOPE, backend, event
from tests.test_memory_lifecycle import EXPIRED, confirm


@pytest.mark.parametrize("protocol", ["text", "openai-completions", "openai-responses"])
@pytest.mark.parametrize("invalid", [False, True])
def test_same_call_metadata_cannot_break_normal_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                     protocol: str, invalid: bool) -> None:
    sent: list[JsonObject] = []
    task = "Always use Python examples."
    candidate: JsonObject = {"kind": "preference", "text": task, "quote": task,
        "quote_start": 0, "quote_end": len(task), "slot": "code.language", "value": "python"}
    valid_metadata: list[JsonValue] = [candidate]
    metadata: JsonValue = "invalid metadata" if invalid else valid_metadata

    class Response(BytesIO):
        headers = {"Content-Type": "application/json"}

    def send(request: Request, timeout: float) -> Response:
        assert isinstance(request.data, bytes)
        request_payload: object = json.loads(request.data)
        sent.append(decode_json_object(request_payload))
        args: JsonObject = {"summary": "Understood.", "memory_candidates": metadata}
        text_reply: JsonObject = {"tool": "done", "args": args}
        message: JsonObject = {"role": "assistant", "content": json.dumps(text_reply)}
        if protocol != "text":
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call-finish", "type": "function", "function": {"name": "done", "arguments": json.dumps(args)}}]}
        reply: JsonObject = {"choices": [{"finish_reason": "stop" if protocol == "text" else "tool_calls", "message": message}]}
        if protocol == "openai-responses":
            reply = {"id": "response-finish", "status": "completed", "output": [
                {"type": "function_call", "id": "function-finish", "call_id": "call-finish", "name": "done", "arguments": json.dumps(args)}]}
        return Response(json.dumps(reply).encode())

    monkeypatch.setattr(api_transport, "open_request", send)

    def connect(provider_id: str, *, port: int) -> ChatProvider:
        return ApiProvider("http://fixture.invalid/v1", "fixture", native_tools=protocol != "text",
            text_tool_format="json_schema" if protocol == "text" else "plain",
            api_protocol="openai-responses" if protocol == "openai-responses" else "openai-completions",
            stream=False, thinking_enabled=False)

    events: list[dict[str, object]] = []
    result = run_headless(HeadlessRequest(None, task, intent="chat", provider_id="qwen", session_id="s",
        max_turns=1, state_home=tmp_path), emit_jsonl=events.append, connect_provider=connect)
    assert result.exit_code == 0, events[-1]
    assert len(sent) == 1
    assert "memory_candidates" in json.dumps(sent[0])
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        proposals = state.memory.proposals(SCOPE)
        assert len(proposals) == (0 if invalid else 1)
        if proposals:
            assert proposals[0].item.value == "python"
            assert memory_action(state, {"action": "accept", "session_id": "s", "proposal_id": proposals[0].proposal_id,
                                         "version": proposals[0].version})[0] == 200
        before = state.memory.export_state()
        state.memory.rebuild()
        assert state.memory.export_state() == before


def promote(store_path: Path, *, scope: str = SCOPE) -> None:
    store = backend(store_path)
    evaluation = MemoryEvaluation("evaluation:fixture", "code_examples", "chat", "a" * 64, "b" * 64, "c" * 64,
                                   0, 2, 2, 4, 4, 0, True)
    store.observe(MemoryEvent("report:fixture", "memory.evaluated", NOW, "", "", "", scope,
                             evaluation.to_payload(), ("user:report",)))
    store.family_change(MemoryFamilyChange("code_examples", "chat", scope, "demoted", "family:demote", NOW))
    for state in ("shadow_candidate", "production_candidate", "enabled"):
        selected = {"shadow_candidate": "shadow_candidate", "production_candidate": "production_candidate", "enabled": "enabled"}[state]
        from codey.memory.contracts import MemoryFamilyState

        states: dict[str, MemoryFamilyState] = {"shadow_candidate": "shadow_candidate", "production_candidate": "production_candidate", "enabled": "enabled"}
        store.family_change(MemoryFamilyChange("code_examples", "chat", scope, states[selected], "family:" + state, NOW,
                                               (evaluation.evaluation_id,) if state != "shadow_candidate" else ()))


def test_family_confirmation_and_promotion_are_independent(tmp_path: Path) -> None:
    store = backend(tmp_path)
    store.observe(replace(event("pref"), kind="memory.proposed", payload={"kind": "preference", "text": "Python", "slot": "code.language", "value": "python"}))
    proposal, = store.proposals(SCOPE)
    from codey.memory.contracts import MemoryDecision

    store.decide(MemoryDecision(proposal.proposal_id, proposal.version, True, "user:confirm", NOW))
    query = MemoryQuery("example", (SCOPE,), NOW, context="chat")
    assert store.retrieve(query).items
    store.family_change(MemoryFamilyChange("code_examples", "chat", SCOPE, "quarantined", "family:initial-q", NOW))
    assert not store.retrieve(query).items
    assert not store.retrieve(replace(query, shadow=True)).items
    store.family_change(MemoryFamilyChange("code_examples", "chat", SCOPE, "shadow_candidate", "family:recover", NOW))
    assert store.retrieve(replace(query, shadow=True)).items
    promote(tmp_path)
    assert store.retrieve(query).items
    store.family_change(MemoryFamilyChange("code_examples", "chat", SCOPE, "quarantined", "family:q", NOW))
    assert store.retrieve(query).items == ()
    before = store.export_state()
    store.rebuild()
    assert store.export_state() == before


def test_snapshot_retention_rebuild_and_corruption_are_atomic(tmp_path: Path) -> None:
    store = backend(tmp_path)
    confirm(store, "persistent")
    for index in range(5):
        store.observe(event("experience:" + str(index)))
    store.compact(MemoryRetentionPolicy(max_experiences_per_scope=2), as_of=EXPIRED)
    before = store.export_state()
    records = store.records((SCOPE,))
    assert sum(row.item.kind == "experience" for row in records) == 2
    assert any(row.item.item_id == "persistent" for row in records)
    first = store.rebuild()
    assert store.export_state() == before
    assert store.rebuild() == first
    store.observe(event("experience:0"))
    assert store.export_state() == before
    with store.database.connect(write=True) as connection:
        connection.execute("UPDATE recovery_base SET payload = '{}' WHERE id = 1")
    with pytest.raises(ValueError, match="corrupt"):
        store.rebuild()
    assert store.export_state() == before


def test_bounded_maintenance_request_is_retryable(tmp_path: Path) -> None:
    store = backend(tmp_path)
    for index in range(3):
        confirm(store, str(index))
    request = MemoryMaintenanceRequest("batch:one", EXPIRED, 1)
    result = store.maintain_batch(request)
    assert result.processed == 1 and not result.complete
    assert store.maintain_batch(request) == result
    second = store.maintain_batch(replace(request, operation_id="batch:two", cursor=result.cursor))
    assert second.processed == 1 and second.cursor != result.cursor
    assert digest(store.export_state())


@pytest.mark.parametrize("overrides", [
    {"quote": "assistant text"}, {"quote_start": -1}, {"quote_end": 500},
    {"quote_start": True}, {"kind": "experience"}, {"extra": "unknown"},
    {"expires_at": "yesterday"}, {"value": "invalid-language"},
])
def test_candidate_rejects_unbound_or_invalid_metadata(overrides: JsonObject) -> None:
    from codey.memory.candidates import candidate_from_payload

    raw: JsonObject = {"kind": "preference", "text": "Use Python", "quote": "Python", "quote_start": 4,
                       "quote_end": 10, "slot": "code.language", "value": "python", **overrides}
    with pytest.raises(ValueError):
        candidate_from_payload(raw, user_text="Use Python", scope=SCOPE, at=NOW)


def test_family_missing_evidence_and_parent_quarantine_block_consumption(tmp_path: Path) -> None:
    store = backend(tmp_path)
    store.family_change(MemoryFamilyChange("code_examples", "chat", SCOPE, "demoted", "family:d", NOW))
    store.family_change(MemoryFamilyChange("code_examples", "chat", SCOPE, "shadow_candidate", "family:s", NOW))
    with pytest.raises(ValueError, match="evaluation"):
        store.family_change(MemoryFamilyChange("code_examples", "chat", SCOPE, "production_candidate", "family:p", NOW))
    store.family_change(MemoryFamilyChange("code_examples", "chat", "user", "quarantined", "family:parent", NOW))
    store.observe(replace(event("pref"), kind="memory.proposed", payload={"kind": "preference", "text": "Python",
        "slot": "code.language", "value": "python"}))
    from codey.memory.contracts import MemoryDecision

    proposal, = store.proposals(SCOPE)
    store.decide(MemoryDecision(proposal.proposal_id, proposal.version, True, "user:confirm", NOW))
    assert not store.retrieve(MemoryQuery("example", (SCOPE,), NOW, context="chat", shadow=True)).items


def test_batch_cursor_is_journal_owned_and_policy_version_is_strict(tmp_path: Path) -> None:
    store = backend(tmp_path)
    for index in range(3):
        confirm(store, str(index))
    result = store.maintain_batch(MemoryMaintenanceRequest("batch:first", EXPIRED, 1))
    assert backend(tmp_path).maintenance_cursor() == result.cursor
    store.rebuild()
    assert store.maintenance_cursor() == result.cursor
    with pytest.raises(ValueError, match="policy version"):
        store.maintain_batch(MemoryMaintenanceRequest("batch:unknown", EXPIRED, policy_version="future"))


def test_snapshot_rejects_stale_commands_outside_retained_retry_window(tmp_path: Path) -> None:
    store = backend(tmp_path)
    confirm(store, "keep")
    store.compact(MemoryRetentionPolicy(keep_events=0), as_of=EXPIRED)
    from codey.memory.contracts import MemoryControl

    with pytest.raises(ValueError, match="retry window"):
        store.set_control(MemoryControl(SCOPE, "admit_hints", False, "old:command", NOW))


@pytest.mark.parametrize("operation", ["install", "truncate"])
def test_snapshot_transaction_failure_preserves_recoverability(tmp_path: Path, operation: str) -> None:
    import sqlite3

    store = backend(tmp_path)
    confirm(store, "keep")
    store.observe(event("old:experience"))
    before = store.export_state()
    trigger = ("CREATE TRIGGER abort_snapshot BEFORE INSERT ON recovery_base BEGIN SELECT RAISE(ABORT, 'fixture crash'); END"
               if operation == "install" else
               "CREATE TRIGGER abort_snapshot BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'fixture crash'); END")
    with store.database.connect(write=True) as connection:
        connection.execute(trigger)
    with pytest.raises(sqlite3.IntegrityError, match="fixture crash"):
        store.compact(MemoryRetentionPolicy(), as_of=EXPIRED)
    with store.database.connect(write=True) as connection:
        connection.execute("DROP TRIGGER abort_snapshot")
    store.rebuild()
    assert store.export_state() == before
    store.compact(MemoryRetentionPolicy(), as_of=EXPIRED)
    assert store.export_state() == before
