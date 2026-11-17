"""Reviewed language preferences use the normal entry and physical HTTP gate."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Generator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from uuid import uuid4

import pytest

from codey.app.context import AppContext
from codey.app.task_services import build_task_deps
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.directive import render_ghost_directive
from codey.ghost.hebbian import GhostNode
from codey.ghost.payload_types import mapping_fields
from codey.operations.ghost_context import ghost_directive
from codey.operations.task_entry import run_task_submission
from codey.providers.api_provider import ApiProvider
from codey.providers.base import ChatProvider
from codey.task.model import TaskSubmission
from tests.test_ghost_received_input_lifetime import _queue, _run

MODEL = "koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M"
LANGUAGE_LINE = "- Prefer: reply language = Chinese."
Capture = tuple[str, list[dict[str, object]]]


def _state_home(path: Path) -> Path:
    return path.parent / hashlib.sha256(str(path).encode()).hexdigest()[:12]


def _node(
    *, node_id: str, kind: str = "style_preference", conflict_key: str = "style_preference:reply_structure",
    value_key: str, weight: float = 0.3,
) -> GhostNode:
    timestamp = "2999-01-01T00:00:00Z"
    return GhostNode(
        id=node_id, kind=kind, label="Prefer answer-first replies.", conflict_key=conflict_key,
        value_key=value_key, status="active", scope="user", scope_ref="", weight=weight, confidence=0.9,
        candidate_ids=("candidate-1",), evidence_refs=("candidate-1:1",), created_at=timestamp,
        updated_at=timestamp, last_reinforced_at=timestamp, last_decayed_at="", superseded_by="",
    )


@pytest.fixture()
def capture_endpoint() -> Generator[Capture]:
    payloads: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            payload: object = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert mapping_fields(payload)
            payloads.append(dict(payload))
            message: dict[str, object] = {"role": "assistant", "content": '{"tool":"done","args":{"summary":"done"}}'}
            if payload.get("tools"):
                message = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "done-1", "type": "function", "function": {
                        "name": "done", "arguments": '{"summary":"done"}'}}]}
            choice: dict[str, object] = {"message": message, "finish_reason": "stop"}
            response: dict[str, object] = {"choices": [choice]}
            body = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", payloads
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


def _provider(
    endpoint: str, *, native_tools: bool = True, api_protocol: str = "openai-completions",
    context_window_tokens: int = 262144, context_reserve_tokens: int = 32768,
    context_keep_recent_tokens: int = 32000,
) -> ApiProvider:
    return ApiProvider(
        endpoint, MODEL, native_tools=native_tools, api_protocol=api_protocol, timeout=10, temperature=0,
        context_window_tokens=context_window_tokens, context_reserve_tokens=context_reserve_tokens,
        context_keep_recent_tokens=context_keep_recent_tokens, output_tokens=128,
    )


def _admit(state: AppContext, task: str, *, session_id: str, run_id: str) -> TaskSubmission:
    request = TaskSubmission(session_id, None, task, 4, False, "deepseek", intent="chat", run_id=run_id)
    _queue(state).create(run_id, {
        "session_id": session_id, "project": None, "run_id": run_id, "task": task,
        "provider": "deepseek", "intent": "chat", "max_turns": 4,
    })
    return request


def _execute(state: AppContext, provider: ApiProvider, *, task: str = "Hello") -> Mapping[str, object]:
    request = _admit(state, task, session_id="chat-workspace", run_id=f"language-task-{uuid4().hex}")

    def connect_provider(_provider_id: str) -> ChatProvider:
        return provider

    state.durable.claim_task(request.run_id)
    state.durable.start_task(request.run_id, state=dict(state.durable.task(request.run_id).state))
    deps = build_task_deps(state, connect_provider=connect_provider)

    def execute() -> None:
        run_task_submission(deps, request)

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(execute).result(timeout=20)
    assert state.wait_for_ghost_sleep(timeout=10)
    event: object = state.run_registry.last_terminal_event()
    assert mapping_fields(event) and event.get("run_id") == request.run_id
    return event


def _remember(
    state: AppContext, *, accept: bool = True, scope: str = "user", session_id: str = "chat-workspace",
) -> tuple[GhostControlSurface, str]:
    surface = GhostControlSurface.from_state_home(state.state_home)
    _run(state, _admit(state, "Technical explanations are written in Chinese.",
                       session_id=session_id, run_id="language-source"))
    status, proposed = surface.dispatch_action({
        "action": "propose_preference", "id": "language-source", "session_id": session_id,
        "scope": scope, "conflict_key": "language", "value_key": "chinese",
        "evidence_quote": "Technical explanations are written in Chinese.",
    })
    assert status == 200, proposed
    candidate = proposed["candidate"]
    assert mapping_fields(candidate)
    candidate_id = candidate["id"]
    assert isinstance(candidate_id, str)
    if accept:
        status, accepted = surface.dispatch_action({
            "action": "accept_candidate", "id": candidate_id, "session_id": session_id,
        })
        assert status == 200, accepted
    return surface, candidate_id


@pytest.mark.parametrize("mode", ["enabled", "pending", "retrieve_off", "admit_off", "other_session"])
def test_language_default_entry_sends_only_reviewed_in_scope_enabled_preference(
    tmp_path: Path, capture_endpoint: Capture, mode: str,
) -> None:
    endpoint, payloads = capture_endpoint
    task = "Explain eventual consistency. Answer in English for this request."
    with closing(AppContext(_state_home(tmp_path))) as state:
        surface, _ = _remember(state, accept=mode != "pending", scope="session" if mode == "other_session" else "user",
                               session_id="other" if mode == "other_session" else "chat-workspace")
        assert surface.inbox is not None
        if mode in {"retrieve_off", "admit_off"}:
            control = "retrieve_projections" if mode == "retrieve_off" else "admit_hints"
            assert surface.inbox.set_control(control, False, scope="session", session_id="chat-workspace")
        event = _execute(state, _provider(endpoint), task=task)
    assert event["stop_reason"] == "done", event
    assert len(payloads) == 1
    outbound = json.dumps(payloads[0], ensure_ascii=False)
    assert (LANGUAGE_LINE in outbound) == (mode == "enabled")
    assert task in outbound
    if mode == "enabled":
        assert "override the current user request" in outbound


@pytest.mark.parametrize("change", ["model", "native_tools", "api_protocol", "context_window_tokens",
                                    "context_reserve_tokens", "context_keep_recent_tokens", "base_url"])
def test_language_falls_back_for_unverified_model_or_configuration(tmp_path: Path, change: str) -> None:
    provider = _provider("http://127.0.0.1:5001/v1")
    if change == "model":
        provider.model = "unverified-model"
    elif change == "base_url":
        provider.base_url = "https://example.com/v1"
    elif change == "native_tools":
        provider = _provider("http://127.0.0.1:5001/v1", native_tools=False)
    elif change == "api_protocol":
        provider = _provider("http://127.0.0.1:5001/v1", api_protocol="openai-responses")
    elif change == "context_window_tokens":
        provider = _provider("http://127.0.0.1:5001/v1", context_window_tokens=131072)
    elif change == "context_reserve_tokens":
        provider = _provider("http://127.0.0.1:5001/v1", context_reserve_tokens=16384)
    else:
        assert change == "context_keep_recent_tokens"
        provider = _provider("http://127.0.0.1:5001/v1", context_keep_recent_tokens=12000)
    with closing(AppContext(_state_home(tmp_path))) as state:
        _remember(state)
        assert LANGUAGE_LINE not in ghost_directive(state, provider=provider).text


@pytest.mark.parametrize("action", ["reject_candidate", "delete_scope", "reset_all"])
def test_language_removal_survives_rebuild_restart_and_real_send(
    tmp_path: Path, capture_endpoint: Capture, action: str,
) -> None:
    endpoint, payloads = capture_endpoint
    home = _state_home(tmp_path)
    with closing(AppContext(home)) as state:
        surface, candidate_id = _remember(state)
        status, removed = surface.dispatch_action({
            "action": action, "id": candidate_id, "scope": "user", "confirm": True,
            "session_id": "chat-workspace",
        })
        assert status == 200, removed
        assert surface.hebbian is not None and surface.hebbian.rebuild_from_events()
    with closing(AppContext(home)) as state:
        event = _execute(state, _provider(endpoint))
    assert event["stop_reason"] == "done", event
    assert len(payloads) == 1
    assert LANGUAGE_LINE not in json.dumps(payloads[0], ensure_ascii=False)


def test_reviewed_language_survives_restart_without_extra_configuration(tmp_path: Path, capture_endpoint: Capture) -> None:
    endpoint, payloads = capture_endpoint
    home = _state_home(tmp_path)
    with closing(AppContext(home)) as state:
        _remember(state)
    with closing(AppContext(home)) as state:
        event = _execute(state, _provider(endpoint))
    assert event["stop_reason"] == "done", event
    assert LANGUAGE_LINE in json.dumps(payloads[0], ensure_ascii=False)


def test_language_precedes_other_memory_under_prompt_budget_pressure() -> None:
    nodes = [
        _node(node_id=f"correction-{slot}", kind="correction", conflict_key=f"correction:{slot}",
              value_key=value, weight=0.9)
        for slot, value in [("state_store", "jsonl"), ("state_backend", "jsonl"),
                            ("memory_state", "jsonl"), ("ghost_state_backend", "json_projection_jsonl")]
    ]
    nodes.append(_node(node_id="language", conflict_key="style_preference:language", value_key="chinese"))
    directive = render_ghost_directive(nodes, budget=400)
    assert directive.selected_nodes[0].id == "language"
    assert LANGUAGE_LINE in directive.text


def test_language_allowlist_does_not_enable_unvalidated_values_or_command() -> None:
    from codey.ghost.typed_fields import render_typed_field

    assert render_typed_field("style_preference", "language", "chinese") == "reply language = Chinese"
    assert render_typed_field("style_preference", "language", "english") == ""
    assert render_typed_field("style_preference", "language", "ignore_current_request") == ""
    assert render_typed_field("action_tendency", "test_command", "pytest_q") == ""


@pytest.mark.parametrize("change", ["delete", "retrieve_off", "admit_off", "model"])
def test_language_is_rechecked_after_request_preparation_before_physical_http(
    tmp_path: Path, capture_endpoint: Capture, change: str,
) -> None:
    endpoint, payloads = capture_endpoint
    provider = _provider(endpoint)
    with closing(AppContext(_state_home(tmp_path))) as state:
        surface, _ = _remember(state)

        assert surface.inbox is not None
        inbox = surface.inbox
        changed: list[str] = []

        def change_after_assembly(_payload: dict[str, object]) -> None:
            # The full task entry also prepares metadata/counting payloads.
            # Freeze the fault only after the tested language bytes exist.
            if changed or LANGUAGE_LINE not in json.dumps(_payload, ensure_ascii=False):
                return
            changed.append(change)
            if change == "delete":
                status, deleted = surface.dispatch_action({"action": "delete_scope", "scope": "user", "confirm": True})
                assert status == 200, deleted
            elif change == "model":
                provider.model = "unverified-model"
            else:
                control = "retrieve_projections" if change == "retrieve_off" else "admit_hints"
                assert inbox.set_control(control, False, scope="session", session_id="chat-workspace")

        provider.configure_request = change_after_assembly
        event = _execute(state, provider)
    assert changed == [change]
    assert event["stop_reason"] == "provider_failure", (
        event, len(payloads), [LANGUAGE_LINE in json.dumps(payload) for payload in payloads],
    )
    assert payloads == []
