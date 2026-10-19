"""The SQLite backend keeps row decoding in one explicit projection owner."""

from __future__ import annotations

import json
import sqlite3

from codey.durable.sqlite_records import (
    artifact_from_row,
    effect_from_row,
    entry_from_row,
    submission_from_row,
    task_from_row,
)


def _row(sql: str, params: tuple[object, ...] = ()) -> sqlite3.Row:
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    try:
        return database.execute(sql, params).fetchone()
    finally:
        database.close()


def test_durable_rows_have_one_shared_decoder() -> None:
    task = task_from_row(_row(
        "SELECT 'task' id, 'chat' conversation_id, 'submission' submission_id, "
        "'accepted' phase, 0 revision, ? provider_snapshot, ? state, "
        "NULL current_turn_id, NULL pending_effect_id, 'pending' status, 1 schema_version",
        (json.dumps({"provider_id": "test"}), json.dumps({})),
    ))
    assert task.task_id == "task"
    assert task.provider_snapshot == {"provider_id": "test"}

    effect = effect_from_row(_row(
        "SELECT 'effect' id, 'task' task_id, 'shell' category, 'unsafe' replay_class, "
        "'digest' args_digest, 'pending' status, ? intent, NULL settlement",
        (json.dumps({"command": "echo ok"}),),
    ))
    assert effect.effect_id == "effect"
    assert effect.intent == {"command": "echo ok"}

    artifact = artifact_from_row(_row(
        "SELECT 'handle' handle, ? sha256, 3 byte_count, '/tmp/out' storage_path, "
        "'task' owner_task_id, NULL owner_effect_id, 'now' created_at, ? metadata",
        ("a" * 64, json.dumps({"kind": "text"})),
    ))
    assert artifact.handle == "handle"
    assert artifact.metadata == {"kind": "text"}

    entry = entry_from_row(_row(
        "SELECT 'entry' id, 'chat' conversation_id, NULL parent_id, 'user_message' kind, "
        "? payload, 4 commit_seq, NULL by_task_id",
        (json.dumps({"text": "hello"}),),
    ))
    assert entry.entry_id == "entry"
    assert entry.payload == {"text": "hello"}

    submission = submission_from_row(_row(
        "SELECT 'sub' id, 'chat' conversation_id, 'request' request_id, NULL entry_id, "
        "NULL answer_entry_id, 'queued' status, NULL reason, NULL detail, 'queued' placement, "
        "'follow_up' mode, ? content",
        (json.dumps({"text": "hello"}),),
    ))
    assert submission.submission_id == "sub"
    assert submission.content == {"text": "hello"}
