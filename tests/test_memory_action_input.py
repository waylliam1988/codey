"""Invalid client action tags must not be reported as backend outages."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.app.memory_routes import memory_action
from codey.types.json_value import JsonValue

INVALID_ACTIONS: tuple[JsonValue, ...] = (
    [], ["rebuild"], {}, {"name": "rebuild"}, None, True, 1, 1.5,
)


@pytest.fixture()
def context(tmp_path: Path) -> Iterator[AppContext]:
    ctx = AppContext(tmp_path, memory_enabled=False)
    ctx.durable.create_conversation("chat")
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.mark.parametrize("action", INVALID_ACTIONS)
def test_non_string_action_is_a_client_error_without_committing_a_command(
    context: AppContext, action: JsonValue,
) -> None:
    before = context.durable.commit_sequence()
    body: dict[str, object] = {"action": action, "session_id": "chat", "scope_kind": "user"}
    status, payload = memory_action(context, body)
    assert status == 400
    assert payload["ok"] is False
    assert context.durable.commit_sequence() == before


def test_unknown_string_action_remains_a_client_error(context: AppContext) -> None:
    before = context.durable.commit_sequence()
    status, payload = memory_action(context, {
        "action": "not-a-memory-command", "session_id": "chat", "scope_kind": "user",
    })
    assert status == 400
    assert payload["ok"] is False
    assert context.durable.commit_sequence() == before
