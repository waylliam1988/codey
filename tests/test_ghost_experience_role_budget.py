"""Both sides of a completed experience survive the actual retrieval budget."""

from __future__ import annotations

import json
from collections.abc import Callable, Generator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

import pytest

from codey.app import provider_services
from codey.app.context import AppContext
from codey.app.task_services import build_task_deps
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.observation_index import (
    RETRIEVED_BLOCK_HEADER,
    render_retrieved_block,
    retrieve_relevant_observations,
)
from codey.ghost.payload_types import mapping_fields
from codey.operations.task_entry import run_task_submission
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ChatProvider
from codey.task.model import TaskSubmission
from tests.test_ghost_received_input_lifetime import _queue

USER = "sharedplan " + "long request details " * 160
MODEL = "fixture-provider-independent-model"
RESULT = "quartzstore chosen because offline durability matters; validation remains pending."


def _row(*, user: str = USER, assistant: str = RESULT, mode: str = "chat") -> dict[str, object]:
    return {"run_id": "past", "user_text": user, "assistant_text": assistant,
            "mode": mode, "ts": "2026-11-18T00:00:00Z"}


@pytest.mark.parametrize("budget", (400, 900, 1800))
@pytest.mark.parametrize("mode", ("chat", "project"))
def test_assistant_only_retrieval_hit_retains_both_sides(budget: int, mode: str) -> None:
    row = _row(mode=mode)
    before = dict(row)
    picked = retrieve_relevant_observations([row], "quartzstore", budget_chars=budget)
    assert picked == (row,)
    rendered = render_retrieved_block(picked, budget)
    assert "sharedplan" in rendered
    assert RESULT in rendered
    assert "user said: " in rendered and "assistant said: " in rendered
    assert "…" in rendered and len(rendered) <= budget
    assert row == before


@pytest.mark.parametrize("long_role", ("user", "assistant"))
def test_short_role_releases_budget_to_long_role(long_role: str) -> None:
    short, long = "ok", "abcdefghijklmnopqrstuvwxyz" * 200
    row = _row(user=long if long_role == "user" else short,
               assistant=long if long_role == "assistant" else short)
    rendered = render_retrieved_block([row], 400)
    assert short in rendered and long[:200] in rendered
    assert len(rendered) == 400


@pytest.mark.parametrize("role", ("user", "assistant"))
def test_one_role_uses_available_content_space(role: str) -> None:
    row = _row(user="x" * 3000 if role == "user" else "",
               assistant="y" * 3000 if role == "assistant" else "")
    rendered = render_retrieved_block([row], 400)
    assert ("x" * 200 if role == "user" else "y" * 200) in rendered
    assert len(rendered) <= 400 and "…" in rendered


def test_tiny_budget_never_silently_drops_one_role() -> None:
    row = _row(user="uu", assistant="aa")
    head = "- [chat 2026-11-18T00:00:00Z] user said: "
    minimum = len(RETRIEVED_BLOCK_HEADER) + 1 + len(head) + len("\n  assistant said: ") + 4
    for budget in range(minimum + 3):
        picked = retrieve_relevant_observations([row], "uu", budget_chars=budget)
        rendered = render_retrieved_block(picked, budget)
        assert len(rendered) <= budget
        assert ("user said: uu" in rendered) == ("assistant said: aa" in rendered)
        assert bool(picked) == (budget >= minimum)


def test_multiple_experiences_preserve_labels_and_whole_block_budget() -> None:
    rows: list[object] = [_row(user=f"sharedplan {i}", assistant=f"quartzstore {i}") for i in range(5)]
    picked = retrieve_relevant_observations(rows, "quartzstore", budget_chars=400, max_items=3)
    rendered = render_retrieved_block(picked, 400)
    assert len(picked) == 3 and len(rendered) <= 400
    assert rendered.count("user said:") == rendered.count("assistant said:") == 3


@dataclass
class Endpoint:
    url: str
    payloads: list[dict[str, object]] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    streams: list[bool] = field(default_factory=list)


@pytest.fixture()
def endpoint() -> Generator[Endpoint]:
    capture = Endpoint("")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            payload: object = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert mapping_fields(payload)
            capture.payloads.append(dict(payload))
            capture.paths.append(self.path)
            capture.streams.append(payload.get("stream") is True)
            arguments: dict[str, str] = {"summary": RESULT}
            text_reply: dict[str, object] = {"tool": "done", "args": arguments}
            message: dict[str, object] = {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"done-{len(capture.payloads)}", "type": "function", "function": {
                    "name": "done", "arguments": json.dumps(arguments)}}]}
            if not payload.get("tools"):
                message = {"role": "assistant", "content": json.dumps(text_reply)}
            response: dict[str, object] = {"choices": [{"message": message, "finish_reason": "stop"}]}
            if self.path.endswith("/responses"):
                output: list[dict[str, object]] = [{"type": "function_call", "id": "fc_done",
                    "call_id": "done-1", "name": "done", "arguments": json.dumps(arguments)}]
                if not payload.get("tools"):
                    output = [{"type": "message", "role": "assistant", "content": [
                        {"type": "output_text", "text": json.dumps(text_reply)}]}]
                response = {"id": "resp_done", "status": "completed", "output": output}
            content_type = "application/json"
            body = json.dumps(response).encode()
            if payload.get("stream") is True:
                content_type = "text/event-stream"
                if self.path.endswith("/responses"):
                    event: dict[str, object] = {"type": "response.completed", "response": response}
                    body = ("data: " + json.dumps(event) + "\n\n").encode()
                else:
                    delta = dict(message)
                    if payload.get("tools"):
                        delta["tool_calls"] = [{"index": 0, "id": "done-1", "type": "function", "function": {
                            "name": "done", "arguments": json.dumps(arguments)}}]
                    chunk: dict[str, object] = {"choices": [{"delta": delta, "finish_reason": "stop"}]}
                    body = ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        capture.url = f"http://127.0.0.1:{server.server_port}/v1"
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield capture
        finally:
            server.shutdown()
            worker.join(timeout=5)
            assert not worker.is_alive()


def _execute(
    state: AppContext, capture: Endpoint, *, task: str, session: str,
    project: str | None = None, intent: str = "chat", identity: str,
    configure: Callable[[ApiProvider], None] | None = None,
    expected_stop: str = "done",
    api_protocol: str = "openai-completions",
) -> Mapping[str, object]:
    request = TaskSubmission(session, project, task, 3, False, "deepseek", intent=intent, run_id=identity)
    _queue(state).create(identity, {
        "session_id": session, "project": project, "run_id": identity, "task": task,
        "provider": "deepseek", "intent": intent, "max_turns": 3, "project_changes_required": False,
    })
    provider = ApiProvider(capture.url, MODEL, native_tools=True, temperature=0, api_protocol=api_protocol,
                           timeout=10, output_tokens=128, context_window_tokens=262144,
                           context_reserve_tokens=32768, context_keep_recent_tokens=32000)
    if configure is not None:
        configure(provider)

    def connect_provider(_identity: str) -> ChatProvider:
        return provider

    state.durable.claim_task(identity)
    state.durable.start_task(identity, state=dict(state.durable.task(identity).state))
    deps = build_task_deps(state, connect_provider=connect_provider)

    def execute() -> None:
        run_task_submission(deps, request)

    def no_external_advisors(_state: object) -> dict[str, bool]:
        return {}

    with patch.object(provider_services, "provider_availability", no_external_advisors), ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(execute).result(timeout=30)
    assert state.wait_for_ghost_sleep(timeout=10)
    event: object = state.run_registry.last_terminal_event()
    assert mapping_fields(event) and event.get("run_id") == identity
    assert event.get("stop_reason") == expected_stop, event
    return event


@pytest.mark.parametrize("consumer", ("chat", "project"))
@pytest.mark.parametrize("boundary", ("enabled", "retrieve_off", "admit_off", "other_project", "deleted"))
def test_normal_entry_preserves_completed_experience_after_restart(
    tmp_path: Path, endpoint: Endpoint, consumer: str, boundary: str,
) -> None:
    home = tmp_path / "state"
    project = tmp_path / "project"
    project.mkdir()
    source_project = str(project) if consumer == "project" else None
    with closing(AppContext(home, start_scheduler=False)) as state:
        _execute(state, endpoint, task=USER, session="source-session", project=source_project, identity="source")
        assert len(endpoint.payloads) == 1
        store = state.ghost_observations
        assert store is not None
        rows = store.read_committed(session_id="source-session")
        assert len(rows) == 1 and rows[0]["assistant_text"] == RESULT
        assert len(str(rows[0]["user_text"])) == 3000
        surface = GhostControlSurface.from_state_home(home)
        assert surface.inbox is not None
        if boundary in {"retrieve_off", "admit_off"}:
            control = "retrieve_projections" if boundary == "retrieve_off" else "admit_hints"
            assert surface.inbox.set_control(control, False)
        elif boundary == "deleted":
            status, payload = surface.dispatch_action({
                "action": "delete_scope", "scope": "session", "session_id": "source-session", "confirm": True,
            })
            assert status == 200, payload
            assert surface.hebbian is not None and surface.hebbian.rebuild_from_events()
            assert surface.continuity is not None and surface.continuity.rebuild_from_events()
    next_project = source_project
    if boundary == "other_project":
        other = tmp_path / "other"
        other.mkdir()
        next_project = str(other)
    session = "new-session" if consumer == "project" else "source-session"
    with closing(AppContext(home, start_scheduler=False)) as state:
        _execute(state, endpoint, task="Explain the quartzstore decision. No changes are required.",
                 session=session, project=next_project, intent=consumer, identity="next")
    assert len(endpoint.payloads) == 2, "Ghost must not add generation or retry calls"
    messages: object = endpoint.payloads[-1]["messages"]
    assert isinstance(messages, list)
    entries: list[object] = messages
    sources: list[str] = []
    for entry in entries:
        assert mapping_fields(entry)
        content = entry.get("content")
        if isinstance(content, str) and RETRIEVED_BLOCK_HEADER in content:
            sources.append(content)
    assert bool(sources) == (boundary == "enabled"), [
        source[source.index(RETRIEVED_BLOCK_HEADER):][:500] for source in sources
    ]
    if boundary == "enabled":
        assert len(sources) == 1
        assert RESULT in sources[0] and "sharedplan" in sources[0]
