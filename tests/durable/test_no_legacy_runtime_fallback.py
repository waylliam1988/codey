from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from codey.app.conversation_runtime import ConversationRuntime
from codey.runtime.core.operation_state import RuntimeOperationStore
from codey.runtime.effects.effect_records import RuntimeEffectStore
from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.runtime.write.task_runtime import TaskRuntime


@pytest.mark.parametrize(
    "factory",
    [RuntimeMutationLine, RuntimeOperationStore, RuntimeEffectStore, ToolResultDeliveryStore],
)
def test_runtime_projection_and_writer_apis_have_no_session_log_fallback(factory: object) -> None:
    parameters = inspect.signature(factory).parameters
    assert "session_log" not in parameters
    assert "durable" in parameters


def test_task_runtime_has_no_session_log_fallback() -> None:
    parameters = inspect.signature(TaskRuntime).parameters
    assert "session_log" not in parameters
    assert "durable" in parameters


def test_conversation_runtime_rejects_missing_durable_without_a_legacy_queue(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="requires a durable session"):
        ConversationRuntime(tmp_path, lambda _event: None)

