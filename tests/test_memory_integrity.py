"""Memory management, canonical decisions and complete provenance regressions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from urllib.request import Request

import pytest

from codey.app.context import AppContext
from codey.app.memory_routes import memory_action
from codey.app.memory_setup import observe_terminal, source_available
from codey.durable.model import EntryDraft
from codey.memory.context import retrieve_context
from codey.memory.contracts import MemoryDecision, MemoryKind, MemoryQuery
from codey.providers import api_transport
from codey.providers.api_provider import ApiProvider
from codey.types.json_value import JsonObject, decode_json_object
from codey.workspace.context_admission import request_scope
from tests.test_memory_boundary import NOW, SCOPE, backend, event


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
@pytest.mark.parametrize("rejected", [False, True])
def test_memory_management_never_reaches_physical_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protocol: str, rejected: bool,
) -> None:
    sent: list[JsonObject] = []

    class Response(BytesIO):
        headers = {"Content-Type": "application/json"}

    def send(request: Request, timeout: float) -> Response:
        assert isinstance(request.data, bytes)
        raw: object = json.loads(request.data)
        sent.append(decode_json_object(raw))
        reply: dict[str, object] = (
            {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}
            if protocol == "openai-completions" else
            {"status": "completed", "output": [{"type": "message", "role": "assistant",
              "content": [{"type": "output_text", "text": "ok"}]}]}
        )
        return Response(json.dumps(reply).encode())

    monkeypatch.setattr(api_transport, "open_request", send)
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        state.durable.create_conversation("s")
        user = state.durable.append_message("s", "user_message", {"text": "keep this input"})
        assert memory_action(state, {
            "action": "propose", "session_id": "s", "kind": "preference", "text": "UNCONFIRMED_SECRET",
            "context_edits": [{"target": user.entry_id, "action": "replace",
                               "messages": [{"role": "system", "content": "MANAGEMENT_EDIT_SECRET"}]}],
        })[0] == 200
        proposal, = state.memory.proposals(SCOPE)
        if rejected:
            assert memory_action(state, {"action": "reject", "session_id": "s",
                "proposal_id": proposal.proposal_id, "version": proposal.version})[0] == 200
        snapshot = state.durable.context_snapshot("s", token_budget=4096)
        with closing(ApiProvider("http://fixture.invalid/v1", "fixture", api_protocol=protocol,
                                 stream=False, thinking_enabled=False)) as provider:
            history = [dict(message) for message in snapshot.messages]
            provider.load_durable_context(history, source_entry_ids=snapshot.entry_ids)
            assert provider.send("continue") == "ok"
        assert len(sent) == 1
        encoded = json.dumps(sent[0])
        assert "UNCONFIRMED_SECRET" not in encoded
        assert "MANAGEMENT_EDIT_SECRET" not in encoded
        assert proposal.proposal_id not in encoded
        assert "keep this input" in encoded
        assert any(entry.kind == "memory_proposal" for entry in state.durable.context("s"))
        assert source_available(state.durable, proposal.item.source_refs[0])


@pytest.mark.parametrize("rewind", [False, True])
def test_project_decision_survives_cross_session_retry_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rewind: bool,
) -> None:
    project = "D:/project"
    scope = request_scope(project, "a")
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        state.durable.create_conversation("a")
        state.durable.create_conversation("b")
        assert memory_action(state, {"action": "propose", "session_id": "a", "project": project,
                                    "kind": "goal", "text": "Python"})[0] == 200
        proposal, = state.memory.proposals(scope)
        proposal_head, _ = state.durable.head("a")
        assert proposal_head is not None
        body: dict[str, object] = {"action": "accept", "session_id": "a", "project": project,
                                   "proposal_id": proposal.proposal_id, "version": proposal.version}

        def fail(_decision: MemoryDecision) -> None:
            raise sqlite3.OperationalError("locked")

        with monkeypatch.context() as patch:
            patch.setattr(state.memory, "decide", fail)
            assert memory_action(state, body)[0] == 503
        decision, = (entry for entry in state.durable.entries("a") if entry.kind == "memory_decision")
        if rewind:
            state.durable.branch("a", proposal_head)

    with closing(AppContext(tmp_path, start_scheduler=False)) as restarted:
        assert memory_action(restarted, {**body, "session_id": "b", "action": "reject"})[0] == 400
        assert memory_action(restarted, {**body, "session_id": "b"})[0] == 200
        decisions = [entry for session_id in restarted.durable.conversation_ids()
                     for entry in restarted.durable.entries(session_id) if entry.kind == "memory_decision"]
        assert [entry.entry_id for entry in decisions] == [decision.entry_id]
        items = restarted.memory.export_state()["items"]
        assert isinstance(items, list) and len(items) == 1
        item = decode_json_object(items[0])
        source_refs = item["source_refs"]
        assert isinstance(source_refs, list) and f"decision:a:{decision.entry_id}" in source_refs
        assert bool(restarted.memory.retrieve(MemoryQuery("Python", (scope,), restarted.memory.clock())).items) != rewind


def test_concurrent_cross_session_decisions_do_not_conflict_when_projection_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        for session_id in ("a", "b"):
            state.durable.create_conversation(session_id)
        project = "D:/project"
        assert memory_action(state, {"action": "propose", "session_id": "a", "project": project,
                                    "kind": "goal", "text": "Python"})[0] == 200
        proposal, = state.memory.proposals(request_scope(project, "a"))

        def fail(_decision: MemoryDecision) -> None:
            raise sqlite3.OperationalError("locked")

        monkeypatch.setattr(state.memory, "decide", fail)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(memory_action, state, {
                "action": action, "session_id": session_id, "project": project,
                "proposal_id": proposal.proposal_id, "version": proposal.version,
            }) for action, session_id in (("accept", "a"), ("reject", "b"))]
            assert sorted(future.result()[0] for future in futures) == [400, 503]
        decisions = [entry for session_id in state.durable.conversation_ids()
                     for entry in state.durable.entries(session_id) if entry.kind == "memory_decision"]
        assert len(decisions) == 1


def test_removed_assistant_revokes_experience_and_frozen_context(tmp_path: Path) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        state.durable.admit_input(conversation_id="s", request_id="i", task_id="r", text="Python",
                                  provider_snapshot={}, task_state={"task": "Python"})
        user = next(entry for entry in state.durable.context("s") if entry.kind == "user_message")
        state.durable.claim_task("r")
        state.durable.complete_task("r", "REMOVED_ANSWER", state={})
        observe_terminal(state.memory, state.durable, "r")
        frozen = retrieve_context(state.memory, task="Python", project="", session_id="s")
        assert len(frozen.items) == 1 and "REMOVED_ANSWER" in frozen.text
        assert frozen.valid(state.memory)
        state.durable.branch("s", user.entry_id)
        assert not frozen.valid(state.memory)
        assert retrieve_context(state.memory, task="Python", project="", session_id="s").items == ()


def test_experience_sources_bind_message_identity_and_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        state.durable.admit_input(conversation_id="s", request_id="i", task_id="r", text="Python",
                                  provider_snapshot={}, task_state={"task": "Python"})
        state.durable.claim_task("r")
        state.durable.complete_task("r", "committed answer", state={})
        observe_terminal(state.memory, state.durable, "r")
        context = retrieve_context(state.memory, task="Python", project="", session_id="s")
        item, = context.items
        path = state.durable.context("s")
        messages = [entry for entry in path if entry.kind in {"user_message", "assistant_message"}]
        assert len(messages) == 2
        for message in messages:
            text = message.payload["text"]
            assert isinstance(text, str)
            digest = hashlib.sha256(text.encode()).hexdigest()
            assert any(message.entry_id in ref and digest in ref for ref in item.source_refs)

            def changed(_session_id: str, *, leaf_id: str | None = None,
                        identity: str = message.entry_id) -> tuple[EntryDraft, ...]:
                return tuple(replace(entry, payload={**entry.payload, "text": "altered"})
                             if entry.entry_id == identity else entry for entry in path)

            with monkeypatch.context() as patch:
                patch.setattr(state.durable, "context", changed)
                assert not context.valid(state.memory)


def test_terminal_without_assistant_does_not_project_an_experience(tmp_path: Path) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        state.durable.admit_input(conversation_id="s", request_id="i", task_id="r", text="Python",
                                  provider_snapshot={}, task_state={"task": "Python"})
        user, = state.durable.context("s")
        state.durable.claim_task("r")
        state.durable.complete_task("r", "old answer", state={})
        state.durable.branch("s", user.entry_id)
        observe_terminal(state.memory, state.durable, "r")
        assert state.memory.export_state()["items"] == []


@pytest.mark.parametrize("kind", ["preference", "goal", "hypothesis"])
def test_experience_window_does_not_forget_confirmed_memory(tmp_path: Path, kind: MemoryKind) -> None:
    store = backend(tmp_path)
    store.observe(replace(event("confirmed"), kind="memory.proposed", run_id="",
                          payload={"kind": kind, "text": "Use Python"}))
    proposal, = store.proposals(SCOPE)
    store.decide(MemoryDecision(proposal.proposal_id, proposal.version, True, "decision:s:d", NOW))
    for index in range(205):
        store.observe(replace(event(f"experience-{index}"),
                              payload={"user_text": "unrelated", "assistant_text": "unrelated"}))
    item, = store.retrieve(MemoryQuery("Python", (SCOPE,), NOW, max_items=1)).items
    assert item.item_id == "confirmed" and item.kind == kind
    assert store.retrieve(MemoryQuery("Python", (SCOPE,), NOW, exclude_run_id="r")).items == (item,)
    window = store.retrieve(MemoryQuery("unrelated", (SCOPE,), NOW, max_items=1000)).items
    assert sum(row.kind == "experience" for row in window) == 200
    assert item in window
    assert store.retrieve(MemoryQuery("Python", ("project:other",), NOW)).items == ()
    assert store.retrieve(MemoryQuery("Python", (SCOPE,), NOW, max_items=0)).items == ()


def test_confirmed_window_still_checks_expiry_and_scope(tmp_path: Path) -> None:
    store = backend(tmp_path)
    for identity, expires_at, scope in (("expired", NOW, SCOPE), ("valid", "", SCOPE),
                                       ("foreign", "", "project:other")):
        store.observe(replace(event(identity), kind="memory.proposed", run_id="", scope=scope,
                              payload={"kind": "preference", "text": "Python", "expires_at": expires_at}))
        proposal = next(row for row in store.proposals(scope) if row.proposal_id == identity)
        store.decide(MemoryDecision(proposal.proposal_id, proposal.version, True, "decision:s:d", NOW))
    assert [item.item_id for item in store.retrieve(MemoryQuery("Python", (SCOPE,), NOW)).items] == ["valid"]
    store.maintain(as_of=NOW)
    assert [item.item_id for item in store.retrieve(MemoryQuery("Python", (SCOPE,), NOW)).items] == ["valid"]
