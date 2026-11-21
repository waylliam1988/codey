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
from codey.providers import api_transport
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ChatProvider
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
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
