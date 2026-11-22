"""Nested evidence must obey the byte budget, not only top-level strings."""

import json
from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.app.control_queries import evidence_page
from codey.control.protocol import ControlError
from codey.durable.model import EntryDraft


def test_nested_tool_evidence_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entry = EntryDraft("e", "c", None, "tool_result", {"data": {"content": "证据" * 300000}}, by_task_id="r")

    def entries(session_id: str) -> tuple[EntryDraft, ...]:
        return (entry,)

    ctx = AppContext(tmp_path, start_scheduler=False, memory_enabled=False)
    try:
        ctx.durable.admit_input(
            conversation_id="c", request_id="request", task_id="r", text="Inspect",
            provider_snapshot={"provider_id": "local"},
        )
        monkeypatch.setattr(ctx.durable, "entries", entries)
        result = evidence_page(ctx, {"conversation_id": "c", "run_id": "r"})
        assert len(json.dumps(result, ensure_ascii=False).encode()) < 128 * 1024
        items = result["items"]
        assert isinstance(items, list)
        item = items[0]
        assert isinstance(item, dict)
        assert item["truncated"] is True
    finally:
        ctx.close()


@pytest.mark.parametrize("wrong_owner", [False, True])
def test_execution_channels_preserve_bounds_and_run_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wrong_owner: bool,
) -> None:
    def snapshot(execution_id: str) -> dict[str, object]:
        return {
            "session_id": "c", "run_id": "other" if wrong_owner else "r",
            "stdout": {"text": "Output", "truncated": False},
            "stderr": {"text": "Error", "truncated": True},
            "execution_state": "settled", "exit_code": 3,
        }

    ctx = AppContext(tmp_path, start_scheduler=False, memory_enabled=False)
    try:
        ctx.durable.admit_input(
            conversation_id="c", request_id="request", task_id="r", text="Inspect",
            provider_snapshot={"provider_id": "local"},
        )
        monkeypatch.setattr(ctx.execution_outputs, "snapshot", snapshot)
        params: dict[str, object] = {
            "conversation_id": "c", "run_id": "r", "execution_id": "execution", "offset": 1, "limit": 1,
        }
        if wrong_owner:
            with pytest.raises(ControlError) as failure:
                evidence_page(ctx, params)
            assert failure.value.code == "NOT_FOUND"
        else:
            result = evidence_page(ctx, params)
            assert result["text"] == "utput\nError"
            assert result["total_chars"] == 12
            assert result["complete"] is True
            assert result["truncated"] is True
            assert result["exit_code"] == 3
    finally:
        ctx.close()
