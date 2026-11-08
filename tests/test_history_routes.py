from __future__ import annotations

import json
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest

from codey.app.history_routes import validate_runs_response
from codey.app.public_event_feed import PublicEventFeed
from codey.durable import DurableSession, SQLiteDurableStore


@dataclass
class Context:
    durable: DurableSession


@pytest.fixture
def context(tmp_path: Path) -> Generator[Context]:
    session = DurableSession(SQLiteDurableStore(tmp_path / "routes.sqlite"))
    try:
        yield Context(session)
    finally:
        session.close()


def test_validation_endpoint_empty_chat_and_database_fence(context: Context) -> None:
    body: dict[str, object] = {"session_id": "new", "epoch": context.durable.database_id,
                               "path_generation": 0, "run_ids": ["unknown"]}
    status, data = validate_runs_response(context, body)
    assert status == 200
    assert data["runs"] == [{"run_id": "unknown", "visible": False}]
    assert validate_runs_response(context, {**body, "epoch": "another-database"})[0] == 409


def test_validation_endpoint_uses_current_version_and_reports_rewrite(context: Context) -> None:
    context.durable.admit_input(conversation_id="chat", request_id="request", task_id="visible", text="input", provider_snapshot={})
    page = context.durable.history_page("chat")
    body = {"session_id": "chat", "epoch": page["epoch"], "path_generation": page["path_generation"],
            "run_ids": ["visible", "unseen"]}
    status, data = validate_runs_response(context, body)
    assert status == 200
    assert data["runs"] == [{"run_id": "visible", "visible": True}, {"run_id": "unseen", "visible": False}]
    context.durable.append_message("chat", "control_config", {"head_marker": True})
    assert validate_runs_response(context, body) == (409, {"ok": False, "error": "history snapshot changed"})


def test_public_sse_encoding_preserves_path_identity_for_a_branch_without_entries(context: Context) -> None:
    context.durable.create_conversation("chat")
    entry = context.durable.append_message("chat", "user_message", {"text": "question"})
    result = context.durable.branch("chat", entry.entry_id)
    assert result.record is not None
    payload: dict[str, object] = json.loads(PublicEventFeed().encode(result.record.to_payload()).body)
    page = context.durable.history_page("chat")
    assert payload["path_generation"] == page["path_generation"] == 1
    assert payload["revision"] == page["snapshot_revision"]
    assert not payload.get("entry_ids")


@pytest.mark.parametrize("field,value", [
    ("session_id", ""), ("epoch", None), ("path_generation", True), ("path_generation", -1),
    ("run_ids", "run"), ("run_ids", [True]), ("run_ids", [""]), ("run_ids", ["x" * 257]),
    ("run_ids", [str(index) for index in range(201)]),
])
def test_validation_endpoint_rejects_unbounded_or_untyped_requests(context: Context, field: str, value: object) -> None:
    body = {"session_id": "chat", "epoch": context.durable.database_id, "path_generation": 0, "run_ids": ["run"]}
    assert validate_runs_response(context, {**body, field: value})[0] == 400
