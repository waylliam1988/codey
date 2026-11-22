"""The bounded answer's truncation flag describes the complete rendered answer."""

from dataclasses import replace
from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.app.control_queries import project_digest, run_snapshot
from codey.durable.model import EntryDraft, TaskCheckpoint


@pytest.mark.parametrize("second_length", [7999, 8000, 8001])
def test_answer_truncation_accounts_for_message_separators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, second_length: int,
) -> None:
    first, second = "a" * 8000, "b" * second_length
    records = (
        EntryDraft("first", "c", None, "assistant_message", {"text": first}, by_task_id="r"),
        EntryDraft("second", "c", "first", "assistant_message", {"text": second}, by_task_id="r"),
    )

    def entries(session_id: str) -> tuple[EntryDraft, ...]:
        return records

    ctx = AppContext(tmp_path, start_scheduler=False, memory_enabled=False)
    try:
        ctx.durable.admit_input(
            conversation_id="c", request_id="request", task_id="r", text="Inspect",
            provider_snapshot={"provider_id": "local"},
        )
        monkeypatch.setattr(ctx.durable, "entries", entries)
        snapshot = run_snapshot(ctx, "c", "r")
        complete_answer = first + "\n" + second
        assert snapshot["answer"] == complete_answer[-16_000:]
        assert snapshot["answer_truncated"] is (len(complete_answer) > 16_000)
    finally:
        ctx.close()


def test_candidate_snapshot_uses_the_covered_project_and_empty_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "source.txt").write_text("Source", encoding="utf-8")
    ctx = AppContext(tmp_path / "state", start_scheduler=False, memory_enabled=False)
    try:
        ctx.durable.admit_input(
            conversation_id="c", request_id="request", task_id="r", text="Inspect",
            provider_snapshot={"provider_id": "local"},
        )
        original = ctx.durable.task("r")
        state: dict[str, object] = {**original.state, "project": str(project)}
        checkpoint = replace(original, state=state)

        def task(run_id: str) -> TaskCheckpoint:
            return checkpoint

        monkeypatch.setattr(ctx.durable, "task", task)
        snapshot = run_snapshot(ctx, "c", "r", candidate=True)
        assert snapshot["candidate_digest"] == project_digest(str(project))
        assert snapshot["verification"] == []
        assert snapshot["changed_files"] == []
        assert snapshot["receipt"] is None
        assert snapshot["reviews"] == []
    finally:
        ctx.close()
