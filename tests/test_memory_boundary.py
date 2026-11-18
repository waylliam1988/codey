"""Production memory ownership, confirmation and provider admission regressions."""
from __future__ import annotations

import ast
import builtins
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.app.memory_routes import memory_action, memory_summary
from codey.app.memory_setup import build_memory, observe_terminal
from codey.ghost.backend import GhostMemory
from codey.memory.context import retrieve_context
from codey.memory.contracts import (
    DeleteScopeRequest,
    MemoryCapabilities,
    MemoryDecision,
    MemoryEvent,
    MemoryHost,
    MemoryItem,
    MemoryQuery,
    MemoryResult,
)
from codey.memory.null import NullMemory, null_capabilities
from codey.memory.service import MemoryService
from codey.operations.kernel_request_builder import KernelRunRequestBuilder
from codey.operations.provider_receipts import restore_context
from codey.runtime.core.cancellation import TaskCancelled
from codey.workspace.context_admission import ContextAdmissionError, admit_context, request_scope
from codey.workspace.context_selection import ContextSpan
from codey.workspace.context_source import ContextCandidate

NOW = "2026-11-19T00:00:00+00:00"
SCOPE = request_scope("", "s")


def backend(root: Path) -> GhostMemory:
    return GhostMemory(MemoryHost(root, lambda: NOW, lambda: None))


def service(store: GhostMemory) -> MemoryService:
    return MemoryService(MemoryCapabilities(store, store, store, store, store),
                         source_available=lambda ref: True, clock=lambda: NOW)


def event(identity: str = "e", *, kind: str = "task.completed") -> MemoryEvent:
    return MemoryEvent(identity, kind, NOW, "s", "", "r", SCOPE,
                       {"user_text": "Python memory", "assistant_text": "Use lexical retrieval"},
                       ("run:r", "conversation:s"))


def test_backend_has_only_protocol_dependencies() -> None:
    root = Path(__file__).resolve().parents[1] / "codey"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        modules.update(alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
        if path.parent.name == "ghost":
            assert not any(name.startswith(("codey.app", "codey.operations", "codey.durable", "codey.runtime"))
                           for name in modules), path
        elif path.name != "memory_setup.py":
            assert not any(name.startswith("codey.ghost") for name in modules), path


def test_core_starts_when_ghost_is_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = builtins.__import__

    def import_without_ghost(name: str, globals: dict[str, object] | None = None,
                             locals: dict[str, object] | None = None,
                             fromlist: tuple[str, ...] = (), level: int = 0) -> object:
        if name.startswith("codey.ghost"):
            raise ModuleNotFoundError("Ghost deliberately absent", name="codey.ghost")
        return original(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_ghost)
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        assert state.memory.retrieve(MemoryQuery("task", (SCOPE,), NOW)).items == ()
        assert state.memory.export_state()["enabled"] is False
        state.durable.admit_input(conversation_id="s", request_id="input", task_id="r",
                                  text="normal task", provider_snapshot={"provider_id": "local"})
        state.durable.claim_task("r")
        state.durable.complete_task("r", "normal result", state={"summary": "normal result"})
        assert state.durable.task("r").phase == "completed"


def test_events_are_atomic_and_idempotent(tmp_path: Path) -> None:
    store = backend(tmp_path)
    store.observe(event())
    store.observe(event())
    assert len(store.retrieve(MemoryQuery("Python", (SCOPE,), NOW)).items) == 1
    bad = replace(event("bad"), payload={"user_text": "bad"})
    with pytest.raises(ValueError):
        store.observe(bad)
    store.observe(event("bad"))
    assert len(store.retrieve(MemoryQuery("Python", (SCOPE,), NOW)).items) == 2
    store.observe(event("failed", kind="task.failed"))
    assert len(store.retrieve(MemoryQuery("Python", (SCOPE,), NOW)).items) == 2


def test_assistant_text_never_confirms_a_preference(tmp_path: Path) -> None:
    store = backend(tmp_path)
    store.observe(replace(event(), payload={"user_text": "Python", "assistant_text": "User always prefers Python"}))
    assert store.proposals(SCOPE) == ()
    assert {item.kind for item in store.retrieve(MemoryQuery("Python", (SCOPE,), NOW)).items} == {"experience"}


def test_exact_proposal_confirmation_and_rejection(tmp_path: Path) -> None:
    store = backend(tmp_path)
    proposal_event = replace(event(), kind="memory.proposed", run_id="", payload={"kind": "preference", "text": "Use Python"})
    store.observe(proposal_event)
    assert store.retrieve(MemoryQuery("Python", (SCOPE,), NOW)).items == ()
    proposal, = store.proposals(SCOPE)
    with pytest.raises(ValueError, match="changed"):
        store.decide(MemoryDecision(proposal.proposal_id, "stale", True, "decision:s:d", NOW))
    store.decide(MemoryDecision(proposal.proposal_id, proposal.version, True, "decision:s:d", NOW))
    item, = store.retrieve(MemoryQuery("", (SCOPE,), NOW)).items
    assert item.kind == "preference"
    assert "decision:s:d" in item.source_refs
    store.observe(replace(proposal_event, event_id="rejected"))
    rejected, = store.proposals(SCOPE)
    store.decide(MemoryDecision(rejected.proposal_id, rejected.version, False, "decision:s:no", NOW))
    assert len(store.retrieve(MemoryQuery("", (SCOPE,), NOW)).items) == 1


@pytest.mark.parametrize("failure", [RuntimeError("read failed"), TimeoutError("read timeout"), sqlite3.DatabaseError("corrupt")])
def test_optional_failures_omit_memory(failure: Exception) -> None:
    class Broken(NullMemory):
        def retrieve(self, query: MemoryQuery) -> MemoryResult:
            raise failure

        def observe(self, event: MemoryEvent) -> None:
            raise failure

        def maintain(self, *, as_of: str) -> None:
            raise failure

    store = Broken()
    memory = MemoryService(MemoryCapabilities(store, store, store, store, store), source_available=lambda ref: True)
    assert memory.retrieve(MemoryQuery("Python", (SCOPE,), NOW)).items == ()
    assert memory.observe(event()) is False
    assert memory.maintain(as_of=NOW) is False


def test_cancellation_propagates() -> None:
    class Cancelled(NullMemory):
        def retrieve(self, query: MemoryQuery) -> MemoryResult:
            raise TaskCancelled("cancelled")

    store = Cancelled()
    memory = MemoryService(MemoryCapabilities(store, store, store, store, store), source_available=lambda ref: True)
    with pytest.raises(TaskCancelled):
        memory.retrieve(MemoryQuery("task", (SCOPE,), NOW))


def test_actual_corrupt_database_does_not_prevent_task_completion(tmp_path: Path) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        path = tmp_path / "memory" / "ghost" / "memory.sqlite3"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not a sqlite database")
        assert retrieve_context(state.memory, task="Python", project="", session_id="s").text == ""
        state.durable.admit_input(conversation_id="s", request_id="input", task_id="r", text="Python",
                                  provider_snapshot={"provider_id": "local"})
        state.durable.claim_task("r")
        state.durable.complete_task("r", "Completed", state={"summary": "Completed"})
        observe_terminal(state.memory, state.durable, "r")
        assert state.durable.task("r").phase == "completed"
        assert memory_action(state, {"action": "propose", "session_id": "s", "kind": "goal", "text": "Python"})[0] == 503


def test_review_routes_and_source_deletion(tmp_path: Path) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        state.durable.create_conversation("s")
        body: dict[str, object] = {"action": "propose", "session_id": "s", "project": "D:/project", "kind": "goal", "text": "Use Python"}
        assert memory_action(state, body)[0] == 200
        proposal, = state.memory.proposals(request_scope("D:/project", "s"))
        query = MemoryQuery("Python", (proposal.item.scope,), state.memory.clock())
        assert state.memory.retrieve(query).items == ()
        assert memory_action(state, {**body, "action": "accept", "proposal_id": proposal.proposal_id, "version": proposal.version})[0] == 200
        assert len(state.memory.retrieve(query).items) == 1
        status, summary = memory_summary(state, {"session_id": ["s"], "project": ["D:/other"]})
        assert status == 200 and summary["items"] == []
        state.durable.delete_conversation("s")
        assert state.memory.retrieve(query).items == ()
        state._delete_session_memory("s")
        assert state.memory.export_state()["items"] == []


@pytest.mark.parametrize("kind", [[], {}, 42, "experience"])
def test_invalid_proposal_kind_is_client_error(tmp_path: Path, kind: object) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        state.durable.create_conversation("s")
        assert memory_action(state, {"action": "propose", "session_id": "s", "kind": kind, "text": "Python"})[0] == 400


def test_source_branch_change_revokes_confirmed_item(tmp_path: Path) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        state.durable.create_conversation("s")
        head = state.durable.append_message("s", "user_message", {"text": "before proposal"})
        assert memory_action(state, {"action": "propose", "session_id": "s", "kind": "preference", "text": "Use Python"})[0] == 200
        proposal, = state.memory.proposals(SCOPE)
        assert memory_action(state, {"action": "accept", "session_id": "s", "proposal_id": proposal.proposal_id, "version": proposal.version})[0] == 200
        context = retrieve_context(state.memory, task="Python", project="", session_id="s")
        assert context.items
        state.durable.branch("s", head.entry_id)
        assert not context.valid(state.memory)


def test_core_scope_expiry_provenance_and_budget() -> None:
    base = MemoryItem("valid", "preference", "Python", SCOPE, ("run:r",), NOW)
    candidates = (base, replace(base, item_id="other", scope="project:foreign"),
                  replace(base, item_id="missing", source_refs=()),
                  replace(base, item_id="expired", expires_at=NOW),
                  replace(base, item_id="invalid", expires_at="garbage"),
                  replace(base, item_id="naive", expires_at="2099-01-01T00:00:00"),
                  replace(base, item_id="bad_created", created_at="bad"),
                  replace(base, item_id="future_created", created_at="2099-01-01T00:00:00+00:00"))

    class Alternative(NullMemory):
        def retrieve(self, query: MemoryQuery) -> MemoryResult:
            return MemoryResult(candidates)

    store = Alternative()
    memory = MemoryService(MemoryCapabilities(store, store, store, store, store), source_available=lambda ref: True, clock=lambda: NOW)
    context = retrieve_context(memory, task="Python", project="", session_id="s")
    assert context.items == (base,)
    assert retrieve_context(memory, task="Python", project="", session_id="s", budget=5).items == ()
    admission = admit_context(context.text, context.sources, request_scope=SCOPE, require_all=True, as_of=NOW)
    assert admission.epoch.admissions[0].source_refs == base.source_refs
    with pytest.raises(ContextAdmissionError):
        admit_context(context.text, (replace(context.sources[0], scope="global"),), request_scope=SCOPE, require_all=True, as_of=NOW)


def test_failed_validation_omits_registered_context_before_freezing() -> None:
    source = ContextCandidate("memory_x", "stale", 5, SCOPE, source_refs=("run:r",))
    request = KernelRunRequestBuilder(provider=object(), run_id="r", effect_scope="writer:1", provider_id="local",
        user_task="task", context_text="before stale after", context_scope=SCOPE, memory_sources=(source,),
        memory_spans=(ContextSpan(source.key, 7, 12),), attempt_validate_memory_sources=lambda actual: False).build()
    assert request.transport.context_text == "before  after"
    assert request.transport.memory_sources == ()
    validate = request.transport.attempt_validate_memory_sources
    assert validate is not None and validate(())


def test_frozen_context_roundtrips_source_refs() -> None:
    source = ContextCandidate("memory_x", "Python", 6, SCOPE, source_refs=("run:r",))
    from dataclasses import asdict

    assert restore_context(json.loads(json.dumps({"text": "Python", "memory_sources": [asdict(source)]}))) == ("Python", (source,))


def test_expiry_maintenance_and_replay_cannot_resurrect_deleted_scope(tmp_path: Path) -> None:
    store = backend(tmp_path)
    store.observe(event())
    assert store.delete_scope(DeleteScopeRequest(SCOPE)).deleted == 1
    store.observe(event())
    assert store.export_state()["items"] == []


def test_null_can_be_injected_without_backend(tmp_path: Path) -> None:
    with closing(AppContext(tmp_path, memory_enabled=False, start_scheduler=False)) as state:
        assert state.memory.export_state()["enabled"] is False
        assert not (tmp_path / "memory").exists()
        assert build_memory(tmp_path, state.durable, enabled=False).retrieve(MemoryQuery("", (SCOPE,), NOW)).items == ()
        assert null_capabilities().reader.retrieve(MemoryQuery("", (SCOPE,), NOW)).items == ()


def test_experience_projects_the_committed_answer_not_internal_summary(tmp_path: Path) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        state.durable.admit_input(conversation_id="s", request_id="input", task_id="r", text="Python",
            provider_snapshot={}, task_state={"task": "Python"})
        state.durable.claim_task("r")
        state.durable.complete_task("r", "Actual user facing answer", state={"summary": "Internal summary"})
        observe_terminal(state.memory, state.durable, "r")
        item, = state.memory.retrieve(MemoryQuery("Python", (SCOPE,), state.memory.clock())).items
        assert "Actual user facing answer" in item.text
        assert "Internal summary" not in item.text


def test_committed_events_still_publish_internal_enums(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with closing(AppContext(tmp_path, memory_enabled=False, start_scheduler=False)) as state:
        state.durable.admit_input(conversation_id="s", request_id="i", task_id="r", text="normal", provider_snapshot={})
        state.durable.claim_task("r")
        state.durable.complete_task("r", "done", state={})
        assert not any("commit publication failed" in record.message for record in caplog.records)


def test_decision_projection_retry_reuses_the_canonical_user_decision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        state.durable.create_conversation("s")
        assert memory_action(state, {"action": "propose", "session_id": "s", "kind": "goal", "text": "Python"})[0] == 200
        proposal, = state.memory.proposals(SCOPE)
        body: dict[str, object] = {"action": "accept", "session_id": "s", "proposal_id": proposal.proposal_id, "version": proposal.version}
        original = state.memory.decide
        def fail(_decision: MemoryDecision) -> None:
            raise sqlite3.OperationalError("locked")
        monkeypatch.setattr(state.memory, "decide", fail)
        assert memory_action(state, body)[0] == 503
        assert memory_action(state, {**body, "action": "reject"})[0] == 400
        monkeypatch.setattr(state.memory, "decide", original)
        assert memory_action(state, body)[0] == 200
        decisions = [row for row in state.durable.context("s") if row.kind == "memory_decision"]
        assert len(decisions) == 1


def test_confidence_survives_proposal_storage_and_confirmation(tmp_path: Path) -> None:
    store = backend(tmp_path)
    store.observe(replace(event(), kind="memory.proposed", run_id="", payload={"kind": "hypothesis", "text": "Python", "confidence": 0.4}))
    proposal, = store.proposals(SCOPE)
    assert proposal.item.confidence == 0.4
    store.decide(MemoryDecision(proposal.proposal_id, proposal.version, True, "decision:s:d", NOW))
    item, = store.retrieve(MemoryQuery("Python", (SCOPE,), NOW)).items
    assert item.confidence == 0.4


def test_chat_uses_an_alternative_memory_implementation_without_extra_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.app import context as composition
    from codey.app.headless_runner import HeadlessRequest, run_headless
    from codey.providers.base import ChatProvider

    class Alternative(NullMemory):
        def retrieve(self, query: MemoryQuery) -> MemoryResult:
            return MemoryResult((MemoryItem("alternative", "preference", "Prefer compact Python examples", SCOPE,
                                            ("example:confirmed",), NOW),))

    alternative = Alternative()
    memory = MemoryService(MemoryCapabilities(alternative, alternative, alternative, alternative, alternative),
                           source_available=lambda ref: True, clock=lambda: NOW)
    def load(_home: Path | None, _durable: object, *, enabled: bool) -> MemoryService:
        return memory
    monkeypatch.setattr(composition, "build_memory", load)

    class Provider:
        name = "Fixture"
        location = "fixture://local"
        def __init__(self) -> None:
            self.prompts: list[str] = []
        def new_chat(self, timeout: float | None = None) -> None:
            pass
        def send(self, text: str, timeout: float | None = None) -> str:
            self.prompts.append(text)
            return json.dumps({"tool": "done", "args": {"summary": "Hello"}})
        def close(self) -> None:
            pass
    provider = Provider()
    def connect(provider_id: str, *, port: int) -> ChatProvider:
        return provider
    rows: list[dict[str, object]] = []
    result = run_headless(HeadlessRequest(project=None, task="Hello", intent="chat", session_id="s",
        provider_id="qwen", max_turns=1, state_home=tmp_path), emit_jsonl=rows.append, connect_provider=connect)
    assert result.exit_code == 0, rows[-1]
    assert len(provider.prompts) == 1
    assert "Prefer compact Python examples" in provider.prompts[0]


def test_concurrent_opposite_decisions_commit_one_canonical_choice(tmp_path: Path) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        state.durable.create_conversation("s")
        status, _ = memory_action(state, {"action": "propose", "session_id": "s",
                                          "kind": "preference", "text": "Use Python"})
        assert status == 200
        proposal, = state.memory.proposals(SCOPE)
        payload: dict[str, object] = {"session_id": "s", "proposal_id": proposal.proposal_id,
                                      "version": proposal.version}
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(memory_action, state, {**payload, "action": action})
                       for action in ("accept", "reject")]
            statuses = [future.result()[0] for future in futures]
        assert sorted(statuses) == [200, 400]
        decisions = [entry for entry in state.durable.context("s") if entry.kind == "memory_decision"]
        assert len(decisions) == 1
        items = state.memory.retrieve(MemoryQuery("Python", (SCOPE,), state.memory.clock())).items
        assert bool(items) == decisions[0].payload["accepted"]


@pytest.mark.parametrize("cancelled", [True, False])
def test_terminal_projection_cancellation_cannot_change_committed_result(tmp_path: Path, cancelled: bool) -> None:
    from threading import Event

    from codey.runtime.core.cancellation import deadline_scope, scope

    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        state.durable.admit_input(conversation_id="s", request_id="r", task_id="r", text="task", provider_snapshot={})
        state.durable.claim_task("r")
        state.durable.complete_task("r", "committed answer", state={})
        stop = Event()
        if cancelled:
            stop.set()
        with scope(stop), deadline_scope(None if cancelled else 0):
            observe_terminal(state.memory, state.durable, "r")
        assert state.durable.task("r").phase == "completed"
