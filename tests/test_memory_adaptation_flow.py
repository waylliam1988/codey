"""Normal headless/API extraction, confirmation, correction and restoration."""

from __future__ import annotations

import json
from contextlib import closing
from io import BytesIO
from pathlib import Path
from urllib.request import Request

import pytest

from codey.app.context import AppContext
from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.app.memory_observation import routing_for_run
from codey.app.memory_routes import memory_action
from codey.memory.context import retrieve_context
from codey.providers import api_transport
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ChatProvider
from codey.types.json_value import JsonObject, JsonValue, decode_json_object, require_json_string
from codey.workspace.context_admission import request_scope


def test_extract_confirm_use_correct_rebuild_delete_through_normal_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    sent: list[bytes] = []
    instructions = ["Always use Python examples.", "Show a greeting.", "For project examples, use Rust instead of Python.", "Show a greeting.", "Show a greeting."]
    scope = request_scope(str(project), "s")
    previous_id = ""

    class Response(BytesIO):
        headers = {"Content-Type": "application/json"}

    def send(request: Request, timeout: float) -> Response:
        assert isinstance(request.data, bytes)
        index = len(sent)
        sent.append(request.data)
        if index == 1:
            assert b'Always use Python examples.' in request.data
        if index == 3:
            assert b'For project examples, use Rust instead of Python.' in request.data
        args: JsonObject = {"summary": "```python\nprint('hello')\n```" if index == 1 else "```rust\nfn main() { println!(\"hello\"); }\n```" if index == 3 else "Recorded."}
        if index in {0, 2}:
            text = instructions[index]
            related_ids: list[JsonValue] = [previous_id] if previous_id else []
            args["memory_candidates"] = [{"kind": "preference", "text": text, "slot": "code.language",
                "value": "python" if index == 0 else "rust", "context": "project", "quote": text,
                "quote_start": 0, "quote_end": len(text), "related_item_ids": related_ids}]
        reply: JsonObject = {"tool": "done", "args": args}
        response: JsonObject = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(reply)}}]}
        return Response(json.dumps(response).encode())

    monkeypatch.setattr(api_transport, "open_request", send)

    def connect(provider_id: str, *, port: int) -> ChatProvider:
        return ApiProvider("http://fixture.invalid/v1", "fixture", native_tools=False, text_tool_format="json_schema",
                           stream=False, thinking_enabled=False)

    run_ids: list[str] = []
    for index, instruction in enumerate(instructions):
        events: list[dict[str, object]] = []
        result = run_headless(HeadlessRequest(project, instruction, intent="chat", provider_id="qwen", session_id="s",
            max_turns=1, state_home=tmp_path / "state"), emit_jsonl=events.append, connect_provider=connect)
        assert result.exit_code == 0, events[-1]
        assert len(sent) == index + 1
        run_ids.append(result.run_id)
        with closing(AppContext(tmp_path / "state", start_scheduler=False)) as state:
            base: dict[str, object] = {"session_id": "s", "project": str(project)}
            if index in {0, 2}:
                proposal, = state.memory.proposals(scope)
                if index == 2:
                    assert any(edge.target_id == previous_id and edge.relation == "conflicting" for edge in proposal.relations)
                assert memory_action(state, {**base, "action": "accept", "proposal_id": proposal.proposal_id,
                    "version": proposal.version, "supersedes": [previous_id] if previous_id else []})[0] == 200
                previous_id = proposal.item.item_id
            if index == 1:
                exposures = state.memory.export_state()["exposures"]
                assert isinstance(exposures, list)
                exposure = next(decode_json_object(row) for row in exposures if isinstance(row, dict) and row.get("item_id") == previous_id)
                assert memory_action(state, {**base, "action": "feedback", "item_id": previous_id, "run_id": result.run_id,
                    "exposure_id": exposure["id"], "rating": "correction"})[0] == 200
            if index == 3:
                assert state.managed_outputs is not None
                frozen = routing_for_run(state.durable, state.managed_outputs, result.run_id)
                first: JsonObject = decode_json_object(frozen[0])
                attempts = first["attempts"]
                assert isinstance(attempts, list) and attempts
                before = state.memory.export_state()
                state.memory.rebuild()
                assert state.memory.export_state() == before
                assert memory_action(state, {**base, "action": "reset", "request_id": "forget-project"})[0] == 200
                state.memory.rebuild()
                assert state.memory.records((scope,)) == ()
                assert routing_for_run(state.durable, state.managed_outputs, result.run_id) == frozen
            if index == 4:
                assert not state.memory.proposals(scope)
    assert len(run_ids) == 5


def test_save_message_selection_without_model_candidate_is_project_bound_and_idempotent(tmp_path: Path) -> None:
    project = str(tmp_path / "project")
    another = str(tmp_path / "another")
    text = "🦀 以后示例用 Rust，重复：以后示例用 Rust"
    quote = "以后示例用 Rust"
    with closing(AppContext(tmp_path / "state", start_scheduler=False)) as state:
        state.durable.admit_input(conversation_id="s", request_id="source", task_id="source",
            text=text, provider_snapshot={}, task_state={"project": project})
        entry_id = require_json_string(state.durable.task("source").state["input_entry_id"], "entry_id")
        state.durable.complete_task("source", "No memory candidate.", state={})
        assistant_id, _ = state.durable.head("s")
        state.durable.admit_input(conversation_id="other-session", request_id="other-source", task_id="other-source",
            text=quote, provider_snapshot={}, task_state={"project": project})
        request: dict[str, object] = {
            "action": "propose_from_message", "session_id": "s", "entry_id": entry_id, "scope_kind": "project",
            "quote": quote, "request_id": "selection", "slot": "code.language", "value": "rust", "context": "project",
        }
        assert memory_action(state, request)[0] == 400
        selection = {**request, "start": text.rindex(quote), "end": len(text)}
        assert memory_action(state, {**selection, "entry_id": assistant_id})[0] == 400
        assert memory_action(state, {**selection, "session_id": "other-session"})[0] == 400
        assert memory_action(state, {**selection, "project": another})[0] == 400
        assert memory_action(state, {**selection, "message_sha256": "wrong"})[0] == 400
        status, proposal = memory_action(state, selection)
        scope = request_scope(project)
        assert status == 200 and proposal["scope"] == scope and proposal["state"] == "pending"
        assert memory_action(state, selection) == (status, proposal)
        assert memory_action(state, {**selection, "start": text.index(quote), "end": text.index(quote) + len(quote)})[0] == 400
        assert state.memory.records((scope,)) == ()
        accept = {"action": "accept", "session_id": "s", "project": project,
                  "proposal_id": proposal["proposal_id"], "version": proposal["version"]}
        assert memory_action(state, accept)[0] == 200
        assert memory_action(state, accept)[0] == 200
        stored, = state.memory.records((scope,))
        assert stored.state == "active" and stored.item.text == quote
        assert any(ref.startswith("message:") and ref.endswith(":" + entry_id) for ref in stored.item.source_refs)
        assert memory_action(state, selection)[1]["state"] == "accepted"
        assert not state.memory.records((request_scope(another), "user"))


@pytest.mark.parametrize("decision_committed", [False, True])
def test_edit_between_selection_and_confirmation_uses_the_core_commit_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decision_committed: bool,
) -> None:
    project = str(tmp_path / "project")
    text = "Use Rust for examples."
    scope = request_scope(project)
    with closing(AppContext(tmp_path / "state", start_scheduler=False)) as state:
        durable = state.durable
        durable.admit_input(conversation_id="s", request_id="source", task_id="source",
            text=text, provider_snapshot={}, task_state={"project": project})
        source = require_json_string(durable.task("source").state["input_entry_id"], "entry_id")
        durable.complete_task("source", "Recorded.", state={})
        status, proposal = memory_action(state, {"action": "propose_from_message", "session_id": "s",
            "project": project, "entry_id": source, "quote": text, "request_id": "select"})
        assert status == 200
        accept = {"action": "accept", "session_id": "s", "project": project,
                  "proposal_id": proposal["proposal_id"], "version": proposal["version"]}
        if decision_committed:
            def projection_unavailable(*args: object) -> None:
                raise OSError("projection interrupted")

            with monkeypatch.context() as patches:
                patches.setattr(state.memory, "decide", projection_unavailable)
                assert memory_action(state, accept)[0] == 503
        _, revision = durable.head("s")
        updated = "Use Go for this example."
        durable.admit_input(conversation_id="s", request_id="edit", task_id="edit", text=updated,
            provider_snapshot={}, task_state={"project": project, "input": {"kind": "edit_user_message",
                "entry_id": source, "expected_revision": revision, "text": updated}})
        assert memory_action(state, accept)[0] == (200 if decision_committed else 400)
        assert memory_action(state, {**accept, "action": "confirm_user_preference", "request_id": "confirm-user",
                                     "text": text})[0] == 400
        assert not state.memory.records(("user",))
        assert not retrieve_context(state.memory, task="Show examples", project=project,
                                    session_id="s", context="project").items
        assert len(state.memory.records((scope,))) == int(decision_committed)


def test_confirmed_project_slot_shadows_user_default_only_where_applicable(tmp_path: Path) -> None:
    project = str(tmp_path / "project")
    other = str(tmp_path / "other")
    with closing(AppContext(tmp_path / "state", start_scheduler=False)) as state:
        base = {"session_id": "s", "project": project}

        def propose(identity: str, language: str) -> tuple[str, str, str]:
            text = f"Use {language} for code examples."
            state.durable.admit_input(conversation_id="s", request_id=identity, task_id=identity, text=text,
                provider_snapshot={}, task_state={"project": project})
            source = require_json_string(state.durable.task(identity).state["input_entry_id"], "entry_id")
            state.durable.complete_task(identity, "Recorded.", state={})
            status, proposal = memory_action(state, {**base, "action": "propose_from_message", "entry_id": source,
                "quote": text, "request_id": "select-" + identity, "slot": "code.language", "value": language})
            assert status == 200
            return (require_json_string(proposal["proposal_id"], "proposal_id"),
                    require_json_string(proposal["version"], "version"), text)

        default_id, default_version, default_text = propose("default", "rust")
        response, confirmed = memory_action(state, {**base, "action": "confirm_user_preference",
            "request_id": "global-pref", "proposal_id": default_id, "version": default_version, "text": default_text})
        assert response == 200
        project_id, project_version, _ = propose("local", "go")
        assert memory_action(state, {**base, "action": "accept", "proposal_id": project_id,
                                     "version": project_version})[0] == 200

        def context(path: str) -> tuple[tuple[str, ...], str, dict[str, str]]:
            view = retrieve_context(state.memory, task="Show a code example.", project=path, session_id="new-chat")
            return (tuple(item.value for item in view.items if item.slot == "code.language"), view.text,
                    {trace.item_id: trace.reason for trace in view.traces})

        selected, prompt, traces = context(project)
        assert selected == ("go",) and "User-confirmed preference" in prompt
        assert traces[require_json_string(confirmed["item_id"], "item_id")] == "slot_shadowed"
        assert context(other)[0] == ("rust",)
        assert memory_action(state, {**base, "action": "revoke", "item_id": project_id, "request_id": "revoke"})[0] == 200
        assert context(project)[0] == ("rust",)
