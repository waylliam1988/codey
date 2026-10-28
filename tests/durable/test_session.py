from __future__ import annotations

from pathlib import Path

import pytest

from codey.durable import (
    CommitConflict,
    DurableSession,
    EffectRecord,
    MemoryDurableStore,
    SQLiteDurableStore,
    SubmissionStatus,
    TaskCheckpoint,
    TaskStatus,
)
from codey.durable.errors import SessionSealedError


def test_admit_input_commits_submission_task_and_user_entry(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))

    first = session.admit_input(
        conversation_id="conversation-1",
        request_id="request-1",
        task_id="task-1",
        text="run the task",
        provider_snapshot={"provider_id": "local", "model_id": "test"},
    )
    retry = session.admit_input(
        conversation_id="conversation-1",
        request_id="request-1",
        task_id="task-retry",
        text="run the task",
        provider_snapshot={"provider_id": "local", "model_id": "test"},
    )

    assert retry == first
    assert [entry.kind for entry in session.entries("conversation-1")] == ["user_message"]
    assert session.task("task-1").phase == "accepted"
    session.close()


def test_admit_input_works_with_memory_backend() -> None:
    session = DurableSession(MemoryDurableStore())
    result = session.admit_input(
        conversation_id="conversation-1",
        request_id="request-1",
        task_id="task-1",
        text="hello",
        provider_snapshot={"provider_id": "test", "model_id": "test"},
    )

    assert result.submission_id is not None
    assert session.entries("conversation-1")[0].payload["text"] == "hello"


def test_task_checkpoint_rejects_unknown_schema_version() -> None:
    with pytest.raises(ValueError, match="unsupported task checkpoint schema"):
        TaskCheckpoint(
            "task-1", "conversation-1", "submission-1", "accepted", 0,
            {"provider_id": "local"}, {}, schema_version=999,
        )


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_fail_task_preserves_pending_effects_when_stopping_scheduler(
    backend: str, tmp_path: Path
) -> None:
    store = (
        MemoryDurableStore()
        if backend == "memory"
        else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    )
    session = DurableSession(store)
    session.admit_input(
        conversation_id="conversation-1",
        request_id="request-1",
        task_id="task-1",
        text="run the task",
        provider_snapshot={"provider_id": "local"},
    )
    session.start_task("task-1", state={})
    session.advance_task(task_id="task-1", phase="running", state={})
    session.advance_task(task_id="task-1", phase="running", state={})
    session.record_effect_intent(
        "task-1",
        EffectRecord(
            "effect-1",
            "task-1",
            "tool_call",
            "unsafe",
            "digest-1",
            "pending",
            {
                "session_id": "conversation-1",
                "run_id": "task-1",
                "lane": "writer",
                "operation_id": "operation-1",
            },
        ),
    )

    session.fail_task("task-1", "worker crashed", state={"summary": "worker crashed"})

    assert session.task("task-1").phase == "failed"
    assert session.task("task-1").state["pending_effect_ids"] == ("effect-1",)
    assert session.task("task-1").pending_effect_id == "effect-1"
    effect = session.effects("task-1")[0]
    assert effect.status == "pending"
    assert effect.settlement is None
    session.close()


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_cancel_task_preserves_pending_effects_and_cancels_submission_atomically(
    backend: str, tmp_path: Path
) -> None:
    store = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    session = DurableSession(store)
    session.admit_input(
        conversation_id="conversation-1", request_id="request-1", task_id="task-1",
        text="write", provider_snapshot={"provider_id": "local"},
    )
    session.start_task("task-1", state={})
    session.advance_task(task_id="task-1", phase="running", state={})
    session.advance_task(task_id="task-1", phase="running", state={})
    session.record_effect_intent(
        "task-1", EffectRecord(
            "effect-1", "task-1", "tool_call", "unsafe", "digest-1", "pending",
            {"lane": "writer", "operation_id": "op-1"},
        )
    )
    session.request_cancel("task-1", reason="user stopped")
    session.cancel_task("task-1", reason="user stopped")
    task = session.task("task-1")
    assert task.status == TaskStatus.CANCELLED
    assert task.state["pending_effect_ids"] == ("effect-1",)
    assert session.effects("task-1")[0].status == "pending"
    assert session.effects("task-1")[0].settlement is None
    assert session.store.submission(task.submission_id).status is SubmissionStatus.CANCELLED
    session.close()


def test_settling_one_effect_keeps_multi_effect_task_running(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    session.admit_input(
        conversation_id="conversation-1", request_id="request-1", task_id="task-1",
        text="two reads", provider_snapshot={"provider_id": "local"},
    )
    session.start_task("task-1", state={})
    session.advance_task(task_id="task-1", phase="running", state={})
    session.advance_task(task_id="task-1", phase="running", state={})
    effects = tuple(
        EffectRecord(
            effect_id,
            "task-1",
            "tool_call",
            "safe",
            f"digest-{index}",
            "pending",
            {
                "session_id": "conversation-1",
                "run_id": "task-1",
                "lane": "writer",
                "operation_id": "operation-1",
            },
        )
        for index, effect_id in enumerate(("effect-1", "effect-2"))
    )
    session.record_effect_intents("task-1", effects)
    session.settle_unknown_effect("task-1", "effect-1")
    assert session.task("task-1").phase == "running"
    assert session.task("task-1").state["pending_effect_ids"] == ("effect-2",)
    session.close()


def test_commit_publication_runs_after_atomic_adoption(tmp_path: Path) -> None:
    publications: list[tuple[int, str, tuple[str, ...]]] = []
    session = DurableSession(
        SQLiteDurableStore(tmp_path / "durable.sqlite3"),
        on_commit=lambda result, mutation: publications.append(
            (result.sequence, mutation.conversation_id, tuple(entry.entry_id for entry in mutation.entries))
        ),
    )
    session.admit_input(
        conversation_id="conversation-1",
        request_id="request-1",
        task_id="task-1",
        text="hello",
        provider_snapshot={"provider_id": "test"},
    )
    assert publications == [(1, "conversation-1", (session.entries("conversation-1")[0].entry_id,))]


def test_commit_survives_process_exit_before_projection_publication(tmp_path: Path) -> None:
    """A crash after SQLite commit cannot erase the durable conversation."""
    path = tmp_path / "durable.sqlite3"

    def crash_before_publish(_result: object, _mutation: object) -> None:
        raise SystemExit("simulated crash before SSE publication")

    session = DurableSession(SQLiteDurableStore(path), on_commit=crash_before_publish)
    with pytest.raises(SystemExit, match="before SSE"):
        session.admit_input(
            conversation_id="conversation-crash",
            request_id="request-crash",
            task_id="task-crash",
            text="committed before publication",
            provider_snapshot={"provider_id": "local"},
        )

    reopened = DurableSession(SQLiteDurableStore(path))
    assert [entry.payload["text"] for entry in reopened.entries("conversation-crash")] == [
        "committed before publication"
    ]
    assert reopened.commits_after(0)


def test_conversation_snapshot_is_durable_metadata_and_not_context_message(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    session = DurableSession(SQLiteDurableStore(path))
    session.create_conversation("conversation-1")
    snapshot = {"mode": "project", "goal": "ship it", "previous_model": "local"}
    entry = session.save_context_snapshot("conversation-1", snapshot)
    assert session.latest_context_snapshot("conversation-1") == entry
    assert session.context_snapshot("conversation-1", token_budget=100).messages == ()
    session.close()
    reopened = DurableSession(SQLiteDurableStore(path))
    assert reopened.latest_context_snapshot("conversation-1").payload == snapshot


def test_task_lifecycle_api_commits_task_and_submission_together(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    session.admit_input(
        conversation_id="conversation-1",
        request_id="request-1",
        task_id="task-1",
        text="hello",
        provider_snapshot={"provider_id": "local", "model_id": "test"},
    )

    session.start_task("task-1", state={"next": "generate"})
    assert session.task("task-1").phase == "running"
    task = session.task("task-1")
    assert session.store.submission(task.submission_id).status is SubmissionStatus.RUNNING

    with pytest.raises(CommitConflict):
        session.finish_task("task-1", "answer", state={"next": "done"})

    session.advance_task(task_id="task-1", phase="running", state={"next": "response"})
    session.advance_task(task_id="task-1", phase="running", state={"next": "complete"})
    session.advance_task(task_id="task-1", phase="completion_pending", state={"next": "commit"})
    session.complete_task("task-1", "answer", state={"next": "done"})
    assert session.task("task-1").phase == "completed"
    assert session.store.submission(task.submission_id).status is SubmissionStatus.DONE
    assert [entry.kind for entry in session.entries("conversation-1")] == ["user_message", "assistant_message"]


def test_effect_intent_and_settlement_are_durable_and_cas_checked(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    session = DurableSession(SQLiteDurableStore(path))
    session.admit_input(
        conversation_id="conversation-1",
        request_id="request-1",
        task_id="task-1",
        text="hello",
        provider_snapshot={"provider_id": "local"},
    )
    session.start_task("task-1", state={"next": "provider"})
    session.advance_task(task_id="task-1", phase="running", state={"next": "response"})
    session.advance_task(task_id="task-1", phase="running", state={"next": "tool"})
    intent = EffectRecord(
        effect_id="effect-1",
        task_id="task-1",
        category="tool_call",
        replay_class="safe",
        args_digest="sha256:abc",
        status="pending",
        intent={"tool": "read_file"},
    )
    session.record_effect_intent("task-1", intent)
    assert session.task("task-1").phase == "running"
    assert session.task("task-1").state["pending_effect_ids"] == ("effect-1",)
    session.settle_effect(
        "task-1",
        EffectRecord(
            effect_id="effect-1",
            task_id="task-1",
            category="tool_call",
            replay_class="safe",
            args_digest="sha256:abc",
            status="settled",
            intent={"tool": "read_file"},
            settlement={"status": "ok", "result_sha256": "sha256:result"},
        ),
    )
    assert session.task("task-1").state["pending_effect_ids"] == ()
    assert session.task("task-1").phase == "running"
    session.close()

    restarted = DurableSession(SQLiteDurableStore(path))
    assert restarted.store.effect("effect-1").settlement == {"status": "ok", "result_sha256": "sha256:result"}
    with pytest.raises(CommitConflict):
        restarted.settle_effect(
            "task-1",
            EffectRecord(
                effect_id="effect-1",
                task_id="task-1",
                category="tool_call",
                replay_class="safe",
                args_digest="sha256:abc",
                status="settled_again",
                intent={"tool": "read_file"},
                settlement={"status": "error"},
            ),
        )


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_effect_batch_and_task_checkpoint_share_one_commit(backend: str, tmp_path: Path) -> None:
    store = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "batch.sqlite3")
    session = DurableSession(store)
    session.admit_input(
        conversation_id="conversation-1", request_id="request-1", task_id="task-1",
        text="inspect both files", provider_snapshot={"provider_id": "local"},
    )
    session.start_task("task-1", state={})
    session.advance_task(task_id="task-1", phase="running", state={})
    session.advance_task(task_id="task-1", phase="running", state={})
    before = session.store.commits_after(0)[-1].sequence

    result = session.record_effect_intents(
        "task-1",
        (
            EffectRecord("effect-1", "task-1", "tool_call", "safe", "digest-1", "pending", {"tool": "read"}),
            EffectRecord("effect-2", "task-1", "tool_call", "safe", "digest-2", "pending", {"tool": "ls"}),
        ),
    )

    checkpoint = session.task("task-1")
    assert result.sequence == before + 1
    assert {effect.effect_id for effect in session.effects("task-1")} == {"effect-1", "effect-2"}
    assert checkpoint.phase == "running"
    assert checkpoint.revision == 4
    assert set(checkpoint.state["pending_effect_ids"]) == {"effect-1", "effect-2"}


def test_effect_batch_failure_rolls_back_task_checkpoint_and_all_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import codey.durable.sqlite as sqlite_store

    path = tmp_path / "batch-rollback.sqlite3"
    session = DurableSession(SQLiteDurableStore(path))
    session.admit_input(
        conversation_id="conversation-1", request_id="request-1", task_id="task-1",
        text="inspect", provider_snapshot={"provider_id": "local"},
    )
    session.start_task("task-1", state={})
    session.advance_task(task_id="task-1", phase="running", state={})
    session.advance_task(task_id="task-1", phase="running", state={})

    def fail_after_task_write(*args: object, **kwargs: object) -> None:
        raise sqlite_store.sqlite3.OperationalError("injected crash")

    monkeypatch.setattr(sqlite_store, "_write_effects", fail_after_task_write)
    with pytest.raises(sqlite_store.sqlite3.OperationalError):
        session.record_effect_intents(
            "task-1",
            (
                EffectRecord("effect-1", "task-1", "tool_call", "safe", "digest-1", "pending", {"tool": "read"}),
                EffectRecord("effect-2", "task-1", "tool_call", "safe", "digest-2", "pending", {"tool": "ls"}),
            ),
        )

    reopened = DurableSession(SQLiteDurableStore(path))
    assert reopened.task("task-1").phase == "running"
    assert reopened.effects("task-1") == ()


def test_provider_effect_keeps_task_lifecycle_running(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    session.admit_input(
        conversation_id="conversation-1",
        request_id="request-1",
        task_id="task-1",
        text="hello",
        provider_snapshot={"provider_id": "local"},
    )
    session.start_task("task-1", state={"next": "provider"})
    intent = EffectRecord(
        effect_id="provider-1",
        task_id="task-1",
        category="provider_send",
        replay_class="unsafe",
        args_digest="sha256:request",
        status="pending",
        intent={"provider": "local"},
    )
    session.record_effect_intent("task-1", intent)
    assert session.task("task-1").phase == "running"
    session.settle_effect(
        "task-1",
        EffectRecord(
            effect_id="provider-1",
            task_id="task-1",
            category="provider_send",
            replay_class="unsafe",
            args_digest="sha256:request",
            status="ok",
            intent={"provider": "local"},
            settlement={"status": "ok", "sent_state": "settled"},
        ),
    )
    assert session.task("task-1").phase == "running"


def test_reviewer_selection_is_durable_and_immutable(tmp_path: Path) -> None:
    from codey.durable.errors import CommitConflict

    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    session.admit_input(
        conversation_id="conversation-1",
        request_id="request-1",
        task_id="task-1",
        text="review",
        provider_snapshot={"provider_id": "local"},
    )
    selection = {"connection_id": "local", "model_id": "reviewer-a", "protocol": "openai-responses"}
    session.record_reviewer_selection("task-1", selection)
    assert dict(session.task("task-1").state["reviewer_selection"]) == selection
    session.record_reviewer_selection("task-1", selection)
    with pytest.raises(CommitConflict, match="reviewer selection is immutable"):
        session.record_reviewer_selection(
            "task-1", {"connection_id": "local", "model_id": "reviewer-b"}
        )


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_same_arguments_across_turns_are_distinct_effects(backend: str, tmp_path: Path) -> None:
    store = MemoryDurableStore() if backend == "memory" else SQLiteDurableStore(tmp_path / "durable.sqlite3")
    session = DurableSession(store)
    session.admit_input(
        conversation_id="conversation-1", request_id="request-1", task_id="task-1",
        text="run the check", provider_snapshot={"provider_id": "local"},
    )
    session.start_task("task-1", state={})
    session.advance_task(task_id="task-1", phase="running", state={})
    session.advance_task(task_id="task-1", phase="running", state={})
    for effect_id in ("effect-1", "effect-2"):
        session.record_effect_intent(
            "task-1",
            EffectRecord(effect_id, "task-1", "tool_call", "safe", "same-digest", "pending", {"tool": "run"}),
        )
        session.settle_effect(
            "task-1",
            EffectRecord(
                effect_id, "task-1", "tool_call", "safe", "same-digest", "settled",
                {"tool": "run"}, {"status": "ok"},
            ),
        )
        if effect_id == "effect-1":
            session.advance_task(task_id="task-1", phase="running", state={})
            session.advance_task(task_id="task-1", phase="running", state={})
    assert {effect.effect_id for effect in session.effects("task-1")} == {"effect-1", "effect-2"}
    if backend == "sqlite":
        session.close()


def test_branch_moves_head_without_deleting_history(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    session.admit_input(
        conversation_id="conversation-1", request_id="request-1", task_id="task-1",
        text="one", provider_snapshot={"provider_id": "local"},
    )
    session.append_message("conversation-1", "assistant_message", {"text": "two"})
    first = session.entries("conversation-1")[0].entry_id
    session.branch("conversation-1", first)
    assert session.head("conversation-1")[0] == first
    assert len(session.entries("conversation-1")) == 2
    session.append_message("conversation-1", "user_message", {"text": "branch"})
    assert [entry.payload["text"] for entry in session.context("conversation-1")] == ["one", "branch"]


def test_storage_failure_seals_session_until_reopened() -> None:
    class FailingStore(MemoryDurableStore):
        fail_writes = False

        def commit(self, mutation):  # type: ignore[no-untyped-def]
            if self.fail_writes:
                raise OSError("injected storage failure")
            return super().commit(mutation)

    store = FailingStore()
    session = DurableSession(store)
    session.create_conversation("conversation-1")
    store.fail_writes = True

    with pytest.raises(OSError, match="injected storage failure"):
        session.append_message("conversation-1", "user_message", {"text": "lost"})

    store.fail_writes = False
    with pytest.raises(SessionSealedError):
        session.append_message("conversation-1", "user_message", {"text": "blocked"})

    reopened = DurableSession(store)
    reopened.append_message("conversation-1", "user_message", {"text": "after reopen"})
    assert [entry.payload["text"] for entry in reopened.context("conversation-1")] == ["after reopen"]


def test_fork_keeps_source_immutable_and_reads_parent_cap(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    session.admit_input(
        conversation_id="conversation-1", request_id="request-1", task_id="task-1",
        text="one", provider_snapshot={"provider_id": "local"},
    )
    cap = session.entries("conversation-1")[0].entry_id
    child = session.fork("conversation-1", cap, child_conversation_id="conversation-2")
    session.append_message(child, "user_message", {"text": "child"})
    assert [entry.payload["text"] for entry in session.context(child)] == ["one", "child"]
    assert [entry.payload["text"] for entry in session.context("conversation-1")] == ["one"]


def test_context_can_read_an_old_child_leaf_without_moving_the_head(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    session.admit_input(
        conversation_id="conversation-1", request_id="request-1", task_id="task-1",
        text="one", provider_snapshot={"provider_id": "local"},
    )
    first = session.append_message("conversation-1", "assistant_message", {"text": "first"})
    second = session.append_message("conversation-1", "assistant_message", {"text": "second"})
    assert [entry.payload["text"] for entry in session.context("conversation-1", leaf_id=first.entry_id)] == ["one", "first"]
    assert session.head("conversation-1")[0] == second.entry_id
