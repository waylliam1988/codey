"""Only recognized actions can resolve a durable question."""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.app.server import CodeyHTTPServer
from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from tests.test_operator_auth_blocks_unauthenticated_http import httpd as httpd
from tools.local_model_gate_shell import GateHTTPClient

_QUESTION_URL = "/api/questions"
_QUESTION_ID = "question-action"
_RUN_ID = "run_question_action"


@pytest.fixture()
def question_client(tmp_path: Path, httpd: CodeyHTTPServer) -> Iterator[GateHTTPClient]:
    ctx = AppContext(tmp_path / "state", start_scheduler=False)
    httpd.app_context = ctx
    try:
        ctx.durable.admit_input(
            conversation_id="chat", request_id="request-action", task_id=_RUN_ID,
            text="Choose the scope", provider_snapshot={"provider_id": "local"},
        )
        ctx.durable.claim_task(_RUN_ID)
        register: Callable[[str, str, str, object, dict[str, object]], object] = ctx.user_questions.request
        question = register(
            _QUESTION_ID, "chat", _RUN_ID,
            {"question": "Choose the scope", "options": ["Read only", "Run tests"]}, {},
        )
        assert decode_json_object(question)["state"] == "pending"
        yield GateHTTPClient(httpd)
    finally:
        ctx.close()


@pytest.mark.parametrize("action", ["unknown", ["cancel"], None])
def test_unrecognized_action_leaves_question_pending(question_client: GateHTTPClient, action: JsonValue) -> None:
    status, payload = question_client.request(
        _QUESTION_URL, {"id": _QUESTION_ID, "revision": 1, "answer": "Read only", "action": action},
    )
    get_status, current = question_client.request(f"{_QUESTION_URL}?id={_QUESTION_ID}")
    assert get_status == 200
    question = decode_json_object(current["question"])
    assert (status, question["state"], question["revision"]) == (400, "pending", 1)
    assert payload["ok"] is False


@pytest.mark.parametrize(("action", "expected"), [(None, "answered"), ("answer", "answered"), ("cancel", "cancelled")])
def test_supported_actions_resolve_question(question_client: GateHTTPClient, action: str | None, expected: str) -> None:
    body: JsonObject = {"id": _QUESTION_ID, "revision": 1, "answer": "Read only"}
    if action is not None:
        body["action"] = action
    status, payload = question_client.request(_QUESTION_URL, body)
    assert status == 202
    question = decode_json_object(payload["question"])
    assert question["state"] == expected
    assert question["run_id"] == _RUN_ID


def test_invalid_answer_returns_the_unchanged_pending_question(question_client: GateHTTPClient) -> None:
    status, payload = question_client.request(
        _QUESTION_URL, {"id": _QUESTION_ID, "revision": 1, "answer": ["Read only"]},
    )
    assert status == 409
    assert payload["error"] == "invalid question answer"
    question = decode_json_object(payload["question"])
    assert question["state"] == "pending"
    assert question["revision"] == 1


def test_list_returns_the_original_pending_question(question_client: GateHTTPClient) -> None:
    status, payload = question_client.request(_QUESTION_URL)
    assert status == 200
    rows = payload["questions"]
    assert isinstance(rows, list) and len(rows) == 1
    assert decode_json_object(rows[0])["id"] == _QUESTION_ID
