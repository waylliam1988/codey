from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import pytest

from codey.durable import (
    CommitConflict,
    CommitResult,
    DurableMutation,
    DurableSession,
    EffectRecord,
    EntryDraft,
    MemoryDurableStore,
    SQLiteDurableStore,
    SubmissionDraft,
    SubmissionStatus,
    SubmissionUpdate,
    TaskCheckpoint,
)


class DurableStore(Protocol):
    def create_conversation(self, conversation_id: str) -> None: ...

    def append(self, draft: EntryDraft, *, expected_revision: int) -> CommitResult: ...

    def submit(self, draft: SubmissionDraft) -> CommitResult: ...

    def entries(self, conversation_id: str) -> tuple[EntryDraft, ...]: ...

    def head(self, conversation_id: str) -> tuple[str | None, int]: ...

    def close(self) -> None: ...

    def commit(self, mutation: DurableMutation) -> CommitResult: ...

    def task(self, task_id: str) -> TaskCheckpoint: ...

    def effect(self, effect_id: str) -> EffectRecord: ...


StoreFactory = Callable[[Path], DurableStore]


@pytest.fixture(params=["memory", "sqlite"])
def store_factory(request: pytest.FixtureRequest) -> StoreFactory:
    if request.param == "memory":
        return lambda _root: MemoryDurableStore()
    return lambda root: SQLiteDurableStore(root / "durable.sqlite3")


def test_append_commits_entry_and_head_as_one_revision(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    store.create_conversation("conversation-1")

    result = store.append(
        EntryDraft("entry-1", "conversation-1", None, "user_message", {"text": "hello"}),
        expected_revision=0,
    )

    assert result.revision == 1
    assert result.sequence == 1
    assert store.head("conversation-1") == ("entry-1", 1)
    assert [entry.entry_id for entry in store.entries("conversation-1")] == ["entry-1"]
    store.close()


def test_memory_entry_payload_is_an_immutable_snapshot(tmp_path: Path) -> None:
    payload: dict[str, object] = {"text": "hello", "parts": [{"kind": "text", "value": "original"}]}
    entry = EntryDraft("entry-1", "conversation-1", None, "user_message", payload)
    payload["text"] = "changed"
    nested = payload["parts"]
    assert isinstance(nested, list)
    nested[0]["value"] = "changed"

    store = MemoryDurableStore()
    store.create_conversation("conversation-1")
    store.append(entry, expected_revision=0)

    saved = store.entries("conversation-1")[0]
    assert saved.payload["text"] == "hello"
    assert saved.payload["parts"] == ({"kind": "text", "value": "original"},)
    with pytest.raises(TypeError):
        saved.payload["text"] = "mutated"  # type: ignore[index]


def test_conversation_creation_is_idempotent_but_rejects_ancestry_reuse(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    store.create_conversation("conversation-1")
    store.create_conversation("conversation-1")

    with pytest.raises(CommitConflict):
        store.create_conversation(
            "conversation-1",
            parent_conversation_id="source",
            parent_entry_id="entry-1",
        )

    store.close()


def test_stale_revision_conflict_writes_nothing(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    store.create_conversation("conversation-1")
    first = EntryDraft("entry-1", "conversation-1", None, "user_message", {"text": "one"})
    store.append(first, expected_revision=0)

    with pytest.raises(CommitConflict):
        store.append(
            EntryDraft("entry-2", "conversation-1", "entry-1", "assistant_message", {"text": "two"}),
            expected_revision=0,
        )

    assert store.head("conversation-1") == ("entry-1", 1)
    assert [entry.entry_id for entry in store.entries("conversation-1")] == ["entry-1"]
    store.close()


def test_submission_request_id_is_idempotent(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    store.create_conversation("conversation-1")
    draft = SubmissionDraft(
        submission_id="submission-1",
        request_id="request-1",
        conversation_id="conversation-1",
        entry=EntryDraft("entry-1", "conversation-1", None, "user_message", {"text": "hello"}),
    )

    first = store.submit(draft)
    assert first.submission_id == "submission-1"
    retry = SubmissionDraft(
        submission_id="submission-retry",
        request_id="request-1",
        conversation_id="conversation-1",
        entry=EntryDraft("entry-retry", "conversation-1", None, "user_message", {"text": "hello"}),
    )
    assert store.submit(retry) == first
    assert first.sequence == 1
    assert store.head("conversation-1") == ("entry-1", 1)
    assert len(store.entries("conversation-1")) == 1
    store.append(
        EntryDraft("entry-2", "conversation-1", "entry-1", "assistant_message", {"text": "hi"}),
        expected_revision=1,
    )
    assert store.submit(retry) == first
    store.close()


def test_submission_request_id_rejects_different_input(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    store.create_conversation("conversation-1")
    first = SubmissionDraft(
        submission_id="submission-1",
        request_id="request-1",
        conversation_id="conversation-1",
        entry=EntryDraft("entry-1", "conversation-1", None, "user_message", {"text": "hello"}),
    )
    store.submit(first)

    from codey.durable import IdempotencyConflict

    with pytest.raises(IdempotencyConflict):
        store.submit(
            SubmissionDraft(
                submission_id="submission-2",
                request_id="request-1",
                conversation_id="conversation-1",
                entry=EntryDraft("entry-2", "conversation-1", "entry-1", "user_message", {"text": "changed"}),
            )
        )

    assert store.head("conversation-1") == ("entry-1", 1)
    assert len(store.entries("conversation-1")) == 1
    store.close()


def test_submission_request_id_rejects_changed_entry_even_with_same_fingerprint(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    from codey.durable import IdempotencyConflict

    store = store_factory(tmp_path)
    store.create_conversation("conversation-1")
    first = SubmissionDraft(
        submission_id="submission-1",
        request_id="request-1",
        conversation_id="conversation-1",
        entry=EntryDraft("entry-1", "conversation-1", None, "user_message", {"text": "hello"}),
        fingerprint="caller-provided-fingerprint",
    )
    store.submit(first)

    retry_with_changed_content = SubmissionDraft(
        submission_id="submission-2",
        request_id="request-1",
        conversation_id="conversation-1",
        entry=EntryDraft("entry-2", "conversation-1", None, "user_message", {"text": "different"}),
        fingerprint="caller-provided-fingerprint",
    )
    with pytest.raises(IdempotencyConflict):
        store.submit(retry_with_changed_content)
    store.close()


@pytest.mark.parametrize("fingerprints", [("result-a", "result-b"), ("result-a", ""), ("", "result-b")])
@pytest.mark.parametrize("stale_revision", [False, True])
def test_commit_rejects_reused_request_with_changed_fingerprint(
    store_factory: StoreFactory, tmp_path: Path, fingerprints: tuple[str, str], stale_revision: bool,
) -> None:
    from codey.durable import IdempotencyConflict

    store = store_factory(tmp_path)
    try:
        store.create_conversation("conversation-1")
        first = EntryDraft("entry-1", "conversation-1", None, "user_message", {"text": "continue"})
        store.submit(SubmissionDraft("submission-1", "request-1", "conversation-1", first, fingerprints[0]))
        retry = EntryDraft("entry-retry", "conversation-1", "entry-1", "user_message", {"text": "continue"})
        with pytest.raises(IdempotencyConflict):
            store.commit(DurableMutation(
                conversation_id="conversation-1",
                expected_revision=0 if stale_revision else 1,
                entries=(retry,),
                submission=SubmissionDraft("retry", "request-1", "conversation-1", retry, fingerprints[1]),
            ))
        assert store.head("conversation-1") == ("entry-1", 1)
        assert len(store.entries("conversation-1")) == 1
    finally:
        store.close()


def test_memory_task_and_effect_payloads_are_immutable_snapshots() -> None:
    store = MemoryDurableStore()
    store.create_conversation("conversation-1")
    entry = EntryDraft("entry-1", "conversation-1", None, "user_message", {"text": "before"})
    store.commit(
        DurableMutation(
            conversation_id="conversation-1",
            expected_revision=0,
            entries=(entry,),
            submission=SubmissionDraft("submission-1", "request-1", "conversation-1", entry),
            task=TaskCheckpoint(
                task_id="task-1",
                conversation_id="conversation-1",
                submission_id="submission-1",
                phase="accepted",
                revision=0,
                provider_snapshot={"options": {"model": "before"}},
                state={"steps": [{"name": "before"}]},
            ),
            effect=EffectRecord(
                effect_id="effect-1",
                task_id="task-1",
                category="shell",
                replay_class="never",
                args_digest="digest-1",
                status="intent",
                intent={"argv": ["echo", "before"]},
            ),
        )
    )

    task = store.task("task-1")
    effect = store.effect("effect-1")
    assert task.state["steps"] == ({"name": "before"},)
    assert effect.intent["argv"] == ("echo", "before")
    with pytest.raises(TypeError):
        task.state["new"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        effect.intent["new"] = True  # type: ignore[index]


def test_task_identity_and_provider_snapshot_are_immutable(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    store.create_conversation("conversation-1")
    initial = TaskCheckpoint(
        task_id="task-1",
        conversation_id="conversation-1",
        submission_id="submission-1",
        phase="accepted",
        revision=0,
        provider_snapshot={"provider_id": "local", "model_id": "model-a"},
        state={"next": "prepare"},
    )
    entry = EntryDraft("entry-1", "conversation-1", None, "user_message", {"text": "hello"})
    store.commit(
        DurableMutation(
            "conversation-1",
            0,
            entries=(entry,),
            submission=SubmissionDraft("submission-1", "request-1", "conversation-1", entry),
            task=initial,
        )
    )

    with pytest.raises(CommitConflict):
        store.commit(
            DurableMutation(
                "conversation-1",
                1,
                task=TaskCheckpoint(
                    task_id="task-1",
                    conversation_id="conversation-1",
                    submission_id="submission-1",
                    phase="running",
                    revision=1,
                    provider_snapshot={"provider_id": "local", "model_id": "model-b"},
                    state={"next": "generate"},
                ),
            )
        )

    assert store.task("task-1") == initial
    store.close()


def test_checkpoint_and_entry_metadata_survive_sqlite_restart(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    session = DurableSession(SQLiteDurableStore(path))
    result = session.admit_input(
        conversation_id="conversation-meta",
        request_id="request-meta",
        task_id="task-meta",
        text="hello",
        provider_snapshot={"provider_id": "local", "model_id": "m"},
    )
    assert result.sequence > 0
    session.advance_task(
        task_id="task-meta",
        phase="dispatching",
        state={"step": 1},
    )
    checkpoint = session.task("task-meta")
    assert checkpoint.current_turn_id is None
    assert checkpoint.pending_effect_id is None
    restarted = DurableSession(SQLiteDurableStore(path))
    assert restarted.task("task-meta").state == checkpoint.state
    assert checkpoint.state["step"] == 1
    assert checkpoint.state["input_entry_id"] == session.entries("conversation-meta")[0].entry_id
    with sqlite3.connect(path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
        assert {"current_turn_id", "pending_effect_id", "status"} <= columns
        entry_columns = {row[1] for row in db.execute("PRAGMA table_info(entries)")}
        assert "by_task_id" in entry_columns
    assert restarted.commits_after(0)


def test_task_cannot_skip_provider_and_response_phases(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    store.create_conversation("conversation-1")
    entry = EntryDraft("entry-1", "conversation-1", None, "user_message", {"text": "hello"})
    store.commit(
        DurableMutation(
            "conversation-1",
            0,
            entries=(entry,),
            submission=SubmissionDraft("submission-1", "request-1", "conversation-1", entry),
            task=TaskCheckpoint(
                "task-1", "conversation-1", "submission-1", "accepted", 0,
                {"provider_id": "local", "model_id": "model"}, {},
            ),
        )
    )
    with pytest.raises(CommitConflict):
        store.commit(
            DurableMutation(
                "conversation-1",
                1,
                task=TaskCheckpoint(
                    "task-1", "conversation-1", "submission-1", "completed", 1,
                    {"provider_id": "local", "model_id": "model"}, {},
                ),
            )
        )
    assert store.task("task-1").phase == "accepted"
    store.close()


def test_provider_effect_can_start_after_tool_outcome(tmp_path: Path) -> None:
    session = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    session.admit_input(
        conversation_id="conversation-turn",
        request_id="request-turn",
        task_id="task-turn",
        text="edit file",
        provider_snapshot={"provider_id": "local"},
    )
    session.start_task("task-turn", state={})
    session.advance_task(task_id="task-turn", phase="running", state={})
    session.advance_task(task_id="task-turn", phase="running", state={})
    session.advance_task(task_id="task-turn", phase="running", state={})
    session.advance_task(task_id="task-turn", phase="running", state={})


def test_effect_settlement_requires_matching_current_status(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    store.create_conversation("conversation-1")
    entry = EntryDraft("entry-1", "conversation-1", None, "user_message", {"text": "hello"})
    store.commit(
        DurableMutation(
            "conversation-1",
            0,
            entries=(entry,),
            submission=SubmissionDraft("submission-1", "request-1", "conversation-1", entry),
            task=TaskCheckpoint(
                task_id="task-1",
                conversation_id="conversation-1",
                submission_id="submission-1",
                phase="accepted",
                revision=0,
                provider_snapshot={"provider_id": "local", "model_id": "model"},
                state={},
            ),
            effect=EffectRecord(
                effect_id="effect-1",
                task_id="task-1",
                category="shell",
                replay_class="never",
                args_digest="digest-1",
                status="intent",
                intent={"command": "echo hi"},
            ),
        )
    )
    with pytest.raises(CommitConflict):
        store.commit(
            DurableMutation(
                "conversation-1",
                1,
                effect=EffectRecord(
                    effect_id="effect-1",
                    task_id="task-1",
                    category="shell",
                    replay_class="never",
                    args_digest="digest-1",
                    status="settled",
                    intent={"command": "echo hi"},
                    settlement={"exit_code": 0},
                ),
                expected_effect_status="running",
            )
        )
    assert store.effect("effect-1").status == "intent"
    store.close()


def test_submission_and_task_lifecycle_share_atomic_commits(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    store.create_conversation("conversation-1")
    user_entry = EntryDraft("entry-user", "conversation-1", None, "user_message", {"text": "hello"})
    store.commit(
        DurableMutation(
            "conversation-1",
            0,
            entries=(user_entry,),
            submission=SubmissionDraft("submission-1", "request-1", "conversation-1", user_entry),
            task=TaskCheckpoint(
                "task-1", "conversation-1", "submission-1", "accepted", 0,
                {"provider_id": "local", "model_id": "model"}, {},
            ),
        )
    )

    store.commit(
        DurableMutation(
            "conversation-1",
            1,
            task=TaskCheckpoint(
                "task-1", "conversation-1", "submission-1", "running", 1,
                {"provider_id": "local", "model_id": "model"}, {},
            ),
            submission_update=SubmissionUpdate(
                "submission-1", "conversation-1", SubmissionStatus.QUEUED, SubmissionStatus.RUNNING,
            ),
        )
    )
    assert store.submission("submission-1").status is SubmissionStatus.RUNNING
    assert store.task("task-1").phase == "running"

    with pytest.raises(CommitConflict):
        store.commit(
            DurableMutation(
                "conversation-1",
                1,
                task=TaskCheckpoint(
                    "task-1", "conversation-1", "submission-1", "running", 2,
                    {"provider_id": "local", "model_id": "model"}, {},
                ),
                submission_update=SubmissionUpdate(
                    "submission-1", "conversation-1", SubmissionStatus.RUNNING, SubmissionStatus.DONE,
                    answer_entry_id="entry-not-in-this-commit",
                ),
            )
        )
    assert store.submission("submission-1").status is SubmissionStatus.RUNNING
    assert store.task("task-1").phase == "running"

    settled = store.commit(
        DurableMutation(
            "conversation-1",
            1,
            task=TaskCheckpoint(
                "task-1", "conversation-1", "submission-1", "failed", 2,
                {"provider_id": "local", "model_id": "model"}, {"reason": "provider_failed"},
            ),
            submission_update=SubmissionUpdate(
                "submission-1", "conversation-1", SubmissionStatus.RUNNING, SubmissionStatus.UNANSWERED,
                reason="provider_failed",
            ),
        )
    )
    assert settled.sequence == 3
    assert store.submission("submission-1").status is SubmissionStatus.UNANSWERED
    assert store.pending_tasks() == ()
    store.close()


def test_sqlite_store_reopens_with_committed_head(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    store = SQLiteDurableStore(path)
    store.create_conversation("conversation-1")
    store.append(
        EntryDraft("entry-1", "conversation-1", None, "user_message", {"text": "hello"}),
        expected_revision=0,
    )
    store.close()

    reopened = SQLiteDurableStore(path)
    assert reopened.head("conversation-1") == ("entry-1", 1)
    assert [entry.entry_id for entry in reopened.entries("conversation-1")] == ["entry-1"]
    reopened.close()


def test_sqlite_records_schema_migration(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    store = SQLiteDurableStore(path)
    store.close()

    import sqlite3

    with sqlite3.connect(path) as database:
        row = database.execute("SELECT MAX(version), COUNT(*) FROM schema_migrations").fetchone()
    assert row == (1, 1)


def test_sqlite_rejects_an_old_durable_database_in_cold_start_mode(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "durable.sqlite3"
    with sqlite3.connect(path) as database:
        database.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        database.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES(1, 'old')"
        )
    with pytest.raises(Exception, match="cold-start|schema"):
        SQLiteDurableStore(path)


def test_sqlite_rejects_partial_database_without_schema_marker(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "partial.sqlite3"
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE commits (seq INTEGER PRIMARY KEY)")
    from codey.durable.errors import SchemaVersionError

    with pytest.raises(SchemaVersionError, match="schema"):
        SQLiteDurableStore(path)


def test_sqlite_initializer_does_not_use_if_not_exists_as_migration() -> None:
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "codey" / "durable" / "sqlite.py").read_text(
        encoding="utf-8"
    )
    assert "CREATE TABLE IF NOT EXISTS schema_migrations" not in source


def test_commit_sequence_is_global_across_conversations(tmp_path: Path) -> None:
    store = SQLiteDurableStore(tmp_path / "durable.sqlite3")
    store.create_conversation("conversation-1")
    store.create_conversation("conversation-2")

    first = store.append(
        EntryDraft("entry-1", "conversation-1", None, "user_message", {"text": "one"}),
        expected_revision=0,
    )
    second = store.append(
        EntryDraft("entry-2", "conversation-2", None, "user_message", {"text": "two"}),
        expected_revision=0,
    )

    assert (first.revision, first.sequence) == (1, 1)
    assert (second.revision, second.sequence) == (1, 2)
    store.close()


def test_task_and_effect_commit_atomically_with_entry(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    store.create_conversation("conversation-1")

    input_entry = EntryDraft("entry-1", "conversation-1", None, "user_message", {"text": "run"})
    result = store.commit(
        DurableMutation(
            conversation_id="conversation-1",
            expected_revision=0,
            entries=(input_entry,),
            submission=SubmissionDraft(
                submission_id="submission-1",
                request_id="request-1",
                conversation_id="conversation-1",
                entry=input_entry,
            ),
            task=TaskCheckpoint(
                task_id="task-1",
                conversation_id="conversation-1",
                submission_id="submission-1",
                phase="accepted",
                revision=0,
                provider_snapshot={"provider_id": "test", "model_id": "model"},
                state={"next": "prepare"},
            ),
        )
    )

    assert result.revision == 1
    assert result.sequence == 1
    assert result.submission_id == "submission-1"
    assert store.task("task-1").phase == "accepted"
    prepared = TaskCheckpoint("task-1", "conversation-1", "submission-1", "running", 1, {"provider_id": "test", "model_id": "model"}, {"next": "request"})
    store.commit(DurableMutation("conversation-1", 1, task=prepared))
    for revision, phase in enumerate(("running", "running"), start=2):
        prepared = TaskCheckpoint("task-1", "conversation-1", "submission-1", phase, revision, {"provider_id": "test", "model_id": "model"}, {"next": "request"})
        store.commit(DurableMutation("conversation-1", 1, task=prepared))
    tool_intent = TaskCheckpoint("task-1", "conversation-1", "submission-1", "running", 4, {"provider_id": "test", "model_id": "model"}, {"next": "tool"})
    store.commit(
        DurableMutation(
            "conversation-1",
            1,
            task=tool_intent,
            effect=EffectRecord("effect-1", "task-1", "shell", "never", "digest-1", "intent", {"command": "echo run"}),
        )
    )
    running = TaskCheckpoint("task-1", "conversation-1", "submission-1", "running", 5, {"provider_id": "test", "model_id": "model"}, {"next": "settle"})
    store.commit(DurableMutation("conversation-1", 1, task=running))

    settled = store.commit(
        DurableMutation(
            conversation_id="conversation-1",
            expected_revision=1,
            task=TaskCheckpoint(
                task_id="task-1",
                conversation_id="conversation-1",
                submission_id="submission-1",
                phase="running",
                revision=6,
                provider_snapshot={"provider_id": "test", "model_id": "model"},
                state={"next": "continue"},
            ),
            effect=EffectRecord(
                effect_id="effect-1",
                task_id="task-1",
                category="shell",
                replay_class="never",
                args_digest="digest-1",
                status="settled",
                intent={"command": "echo run"},
                settlement={"exit_code": 0},
            ),
            expected_effect_status="intent",
        )
    )
    assert settled.sequence == 7
    assert store.task("task-1").phase == "running"
    assert store.effect("effect-1").settlement == {"exit_code": 0}


def test_failed_task_commit_leaves_entry_and_runtime_facts_unchanged(
    store_factory: StoreFactory, tmp_path: Path
) -> None:
    store = store_factory(tmp_path)
    store.create_conversation("conversation-1")

    with pytest.raises(CommitConflict):
        store.commit(
            DurableMutation(
                conversation_id="conversation-1",
                expected_revision=0,
                entries=(EntryDraft("entry-1", "conversation-1", None, "user_message", {"text": "run"}),),
                effect=EffectRecord(
                    effect_id="effect-1",
                    task_id="missing-task",
                    category="shell",
                    replay_class="never",
                    args_digest="digest-1",
                    status="intent",
                    intent={"command": "echo run"},
                ),
            )
        )

    assert store.head("conversation-1") == (None, 0)
    assert store.entries("conversation-1") == ()
    with pytest.raises(KeyError):
        store.task("task-1")
    store.close()
