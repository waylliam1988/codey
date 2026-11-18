"""Purpose-bound history preserves role identity and complete semantic units."""

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
from codey.operations.kernel_contracts import KernelExecutionDeps, KernelObservationDeps, KernelRunRequest
from codey.operations.kernel_request_builder import KernelRunRequestBuilder
from codey.operations.task_entry import run_task_submission
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ChatProvider
from codey.task.model import TaskSubmission
from tests.test_ghost_received_input_lifetime import _queue

USER = "For sharedplan compare quartzstore and opallog. Do not decide for me."
MODEL = "fixture-provider-independent-model"
RESULT = "quartzstore chosen because offline durability matters; validation remains pending."


def _row(*, user: str = USER, assistant: str = RESULT, mode: str = "chat") -> dict[str, object]:
    return {"run_id": "past", "user_text": user, "assistant_text": assistant,
            "mode": mode, "ts": "2026-11-18T00:00:00Z"}


@pytest.mark.parametrize("budget", (600, 900, 1800))
def test_assistant_only_hit_is_consumer_bound(budget: int) -> None:
    row = _row(user="Compare options without making a decision.")
    before = dict(row)
    picked = retrieve_relevant_observations([row], "quartzstore", budget_chars=budget)
    rendered = render_retrieved_block(picked, budget)
    assert picked == (row,) and RESULT in rendered
    assert '"user":' in rendered and '"assistant":' in rendered
    assert '"run":"past"' in rendered and "not user acceptance or verification" in rendered
    assert len(rendered) <= budget and row == before
    assert not retrieve_relevant_observations([row], "quartzstore", view="requests")


@pytest.mark.parametrize("role", ("user", "assistant"))
def test_oversized_role_is_explicitly_omitted_without_clipping_other_role(role: str) -> None:
    long = "Only today do not change the plan. " * 200
    short = "Do not decide for me until I confirm."
    row = _row(user=long if role == "user" else short,
               assistant=long if role == "assistant" else short)
    rendered = render_retrieved_block([row], 600)
    assert short in rendered and long[:50] not in rendered
    assert f'"omitted_roles":["{role}"]' in rendered
    assert "…" not in rendered and len(rendered) <= 600


def test_requests_projection_keeps_conditions_or_omits_whole_request() -> None:
    text = "Only today, when exploring designs, do not decide for me."
    row = _row(user=text)
    full = render_retrieved_block([row], view="requests")
    assert text in full and RESULT not in full
    for budget in range(len(full) + 1):
        rendered = render_retrieved_block([row], budget, view="requests")
        assert len(rendered) <= budget
        assert (text in rendered) == (budget >= len(full))
        assert text[:20] not in rendered or text in rendered


def test_multiple_experiences_have_distinct_source_identity_and_atomic_budget() -> None:
    rows: list[object] = [{**_row(user=f"sharedplan {i}", assistant=f"quartzstore {i}"),
                          "run_id": f"past-{i}"} for i in range(5)]
    picked = retrieve_relevant_observations(rows, "quartzstore", budget_chars=1800, max_items=3)
    rendered = render_retrieved_block(picked, 1800)
    assert len(picked) == 3 and len(rendered) <= 1800
    assert rendered.count('"user":') == rendered.count('"assistant":') == 3
    assert all(f'"run":"past-{i}"' in rendered for i in range(3))


def test_food_history_does_not_reinject_unverified_assistant_claims() -> None:
    user = "你帮我上网看看福州有什么好吃的。"
    old_answer = "扁肉类似肉包子；燕类似肉饼；鼎边糊属于面食；安泰社区食堂不踩雷。"
    row = _row(user=user, assistant=old_answer)
    before = dict(row)
    picked = retrieve_relevant_observations([row], "你帮我去知乎上看看呢", view="requests")
    assert picked == (row,)
    rendered = render_retrieved_block(picked, view="requests")
    assert user in rendered and '"run":"past"' in rendered
    assert all(claim not in rendered for claim in ("肉包子", "肉饼", "面食", "不踩雷"))
    assert row == before


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
        assert rows[0]["user_text"] == USER
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
        _execute(state, endpoint, task="Explain the sharedplan quartzstore comparison. No changes are required.",
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
    assert bool(sources) == (boundary == "enabled" and consumer == "project"), [
        source[source.index(RETRIEVED_BLOCK_HEADER):][:500] for source in sources
    ]
    if boundary == "enabled" and consumer == "project":
        assert len(sources) == 1
        assert "sharedplan" in sources[0]
        assert ('"assistant":' in sources[0]) == (consumer == "project")
    elif boundary == "enabled":
        assert any(mapping_fields(entry) and entry.get("role") == "user" and entry.get("content") == USER
                   for entry in entries), "only dedup when the original canonical input is actually retained"


@pytest.mark.parametrize("change", ("delete", "retrieve_off", "admit_off"))
def test_deduplicated_history_still_rechecks_revocation_and_allows_fresh_recovery(
    tmp_path: Path, endpoint: Endpoint, change: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with closing(AppContext(tmp_path / "state", start_scheduler=False)) as state:
        _execute(state, endpoint, task=USER, session="same", identity="source")
        surface = GhostControlSurface.from_state_home(state.state_home)
        assert surface.inbox is not None
        changed: list[str] = []
        clients: list[ApiProvider] = []
        original = KernelRunRequestBuilder.build
        empty_execution, empty_observation = KernelExecutionDeps(), KernelObservationDeps()

        def configure(provider: ApiProvider) -> None:
            clients.append(provider)

        def build(builder: KernelRunRequestBuilder, *, execution: KernelExecutionDeps = empty_execution,
                  observation: KernelObservationDeps = empty_observation) -> KernelRunRequest:
            request = original(builder, execution=execution, observation=observation)
            if builder.run_id != "revoked":
                return request
            assert request.transport.required_history_refs, "freeze a real deduplication before revoking it"

            def before_send(payload: dict[str, object]) -> None:
                assert RETRIEVED_BLOCK_HEADER not in json.dumps(payload), "the retained user input was deduplicated"
                if changed:
                    return
                changed.append(change)
                if change == "delete":
                    status, result = surface.dispatch_action({"action": "delete_scope", "scope": "session",
                        "session_id": "same", "confirm": True})
                    assert status == 200 and result["ok"], result
                else:
                    assert surface.inbox is not None
                    assert surface.inbox.set_control("retrieve_projections" if change == "retrieve_off" else "admit_hints", False)
            clients[-1].configure_request = before_send
            return request

        monkeypatch.setattr(KernelRunRequestBuilder, "build", build)

        _execute(state, endpoint, task="Explain the sharedplan quartzstore comparison. No changes required.",
            session="same", identity="revoked", configure=configure, expected_stop="provider_failure")
        assert changed == [change] and len(endpoint.payloads) == 1, "revoked projection stops before HTTP"
        _execute(state, endpoint, task="A fresh independent question. No changes required.",
            session="fresh", identity="recovery")
        assert len(endpoint.payloads) == 2
