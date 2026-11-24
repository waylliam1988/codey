"""Client scope validation remains separate from backend failures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.app import memory_routes
from codey.app.context import AppContext
from codey.app.memory_routes import memory_summary
from codey.memory.codec import digest
from codey.memory.contracts import MemoryExposure, MemoryItem
from codey.types.json_value import JsonObject, JsonValue
from codey.workspace.context_admission import request_scope

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


def test_summary_filters_items_and_exposures_without_reordering(context: AppContext, tmp_path: Path) -> None:
    local = request_scope("", "chat")
    foreign = request_scope(str(tmp_path / "other-project"), "")
    items = [MemoryItem(
        item_id=f"item-{index}", kind="preference", text=f"Preference {index}", scope=scope,
        source_refs=("conversation:chat",), created_at="2026-11-24T00:00:00Z",
    ) for index, scope in enumerate((local, foreign, "user", local))]
    item_rows: list[JsonValue] = [item.to_payload() for item in items]
    exposure_rows: list[JsonValue] = [MemoryExposure(
        exposure_id=f"exposure-{index}", item_id=item.item_id, item_version=digest(item.to_payload()),
        run_id=f"run-{index}", request_ref=f"request:artifact_{index:024x}", scope=item.scope,
        occurred_at="2026-11-24T00:00:00Z",
    ).to_payload() for index, item in enumerate(items)]

    def snapshot() -> JsonObject:
        return {"enabled": True, "items": item_rows, "exposures": exposure_rows}

    with patch.object(context.memory, "export_state", snapshot):
        status, payload = memory_summary(context, {"scope_kind": ["session"], "session_id": ["chat"]})
    assert status == 200
    assert payload["items"] == [item_rows[0], item_rows[2], item_rows[3]]
    assert payload["exposures"] == [exposure_rows[0], exposure_rows[2], exposure_rows[3]]
