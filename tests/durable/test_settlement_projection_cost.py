"""Settlement reads project effects without reconstructing every old checkpoint."""

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from codey.durable import DurableSession, EffectRecord, SQLiteDurableStore, timeline
from codey.durable.model import TaskCheckpoint


def test_settlement_query_reconstructs_only_the_checkpoint_at_its_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    durable = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    try:
        durable.admit_input(conversation_id="c", request_id="r", task_id="t", text="check", provider_snapshot={})
        durable.start_task("t", state={})
        durable.advance_task(task_id="t", phase="running", state={})
        intent = EffectRecord("check", "t", "tool_call", "unsafe", "digest", "pending", {"tool": "run"})
        durable.record_effect_intent("t", intent)
        settled = replace(intent, status="ok", settlement={"exit_code": 0})
        durable.settle_effect("t", settled)
        for step in range(16):
            durable.update_task_state("t", {"step": step})

        count = 0
        restore = timeline.checkpoint_from_payload

        def counted_restore(raw: Mapping[str, object]) -> TaskCheckpoint:
            nonlocal count
            count += 1
            return restore(raw)

        monkeypatch.setattr(timeline, "checkpoint_from_payload", counted_restore)
        actual = durable.settled_effects("t")

        assert actual == (settled,)
        # The settlement query needs one checkpoint for the captured task scope,
        # not every prior revision's frozen execution/catalog payload.
        assert count <= 1, f"rebuilt {count} historical checkpoints for one task"
    finally:
        durable.close()
