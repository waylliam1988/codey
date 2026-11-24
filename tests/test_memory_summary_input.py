"""Client scope validation remains separate from backend failures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.app import memory_routes
from codey.app.context import AppContext
from codey.app.memory_routes import memory_summary
from codey.types.json_value import JsonObject

INVALID_QUERIES: tuple[dict[str, list[str]], ...] = (
    {"scope_kind": ["unknown"], "session_id": ["chat"]},
    {"scope_kind": ["project"], "session_id": ["chat"]},
    {"scope_kind": ["session"]},
    {"scope_kind": [""], "session_id": ["chat"]},
)


@pytest.fixture()
def context(tmp_path: Path) -> Iterator[AppContext]:
    ctx = AppContext(tmp_path)
    ctx.durable.create_conversation("chat")
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.mark.parametrize("query", INVALID_QUERIES)
def test_invalid_scope_is_a_client_error_without_changing_core_history(
    context: AppContext, query: dict[str, list[str]],
) -> None:
    before = context.durable.commit_sequence()
    status, payload = memory_summary(context, query)
    assert status == 400
    assert payload["ok"] is False
    assert context.durable.commit_sequence() == before


def test_valid_user_scope_is_available(context: AppContext) -> None:
    status, payload = memory_summary(context, {"scope_kind": ["user"]})
    assert status == 200
    assert payload["ok"] is True
    assert payload["selected_scope"] == "user"


def test_invalid_backend_state_remains_a_service_error(context: AppContext) -> None:
    def invalid_state() -> JsonObject:
        raise ValueError("backend state is malformed")

    with patch.object(context.memory, "export_state", invalid_state):
        status, payload = memory_summary(context, {"scope_kind": ["user"]})
    assert status == 503
    assert payload["ok"] is False


def test_scope_resolution_io_failure_remains_a_service_error(context: AppContext) -> None:
    def unavailable_scope(_payload: JsonObject, _session_id: str, _project: str) -> str:
        raise OSError("scope resolution is unavailable")

    with patch.object(memory_routes, "selected_scope", unavailable_scope):
        status, payload = memory_summary(context, {"scope_kind": ["user"]})
    assert status == 503
    assert payload["ok"] is False
