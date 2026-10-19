from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from codey.durable.ancestry import context_path
from codey.durable.errors import (
    CommitConflict,
    IdempotencyConflict,
    SchedulerOwnershipError,
    SchemaVersionError,
)
from codey.durable.events import mutation_projection
from codey.durable.model import (
    ApprovalRecord,
    ArtifactRecord,
    CommitRecord,
    CommitResult,
    DurableMutation,
    EffectRecord,
    EntryDraft,
    SubmissionDraft,
    SubmissionPlacement,
    SubmissionRecord,
    SubmissionStatus,
    SubmissionUpdate,
    TaskCheckpoint,
    TaskStatus,
    same_submission,
    thaw_json,
    valid_execution_attempt,
    valid_frozen_task_state,
    valid_submission_transition,
    valid_task_status_transition,
    valid_task_transition,
)
from codey.durable.sqlite_records import (
    artifact_from_row,
    effect_from_row,
    entry_from_row,
    submission_from_row,
    task_from_row,
)
from codey.durable.sqlite_schema import initialize_database
from codey.durable.timeline import (
    TaskTransition,
    execution_payload,
    public_commit_payload,
    transition_from_payload,
)


class SQLiteDurableStore:
    """SQLite implementation of the durable append contract."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._database_id = initialize_database(self._connect)

    def _connect(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA synchronous=FULL")
        database.execute("PRAGMA foreign_keys=ON")
        return database

    @property
    def database_id(self) -> str:
        return self._database_id

    def create_conversation(self, conversation_id: str, *, parent_conversation_id: str | None = None, parent_entry_id: str | None = None) -> None:
        if not conversation_id:
            raise ValueError("conversation_id is required")
        with closing(self._connect()) as database:
            existing = database.execute(
                "SELECT parent_conversation_id, parent_entry_id FROM conversations WHERE id=?",
                (conversation_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["parent_conversation_id"] != parent_conversation_id
                    or existing["parent_entry_id"] != parent_entry_id
                ):
                    raise CommitConflict("conversation identity already exists with different ancestry")
                return
            database.execute(
                "INSERT INTO conversations(id, head_entry_id, revision, parent_conversation_id, parent_entry_id) VALUES(?, NULL, 0, ?, ?)",
                (conversation_id, parent_conversation_id, parent_entry_id),
            )

    def append(self, draft: EntryDraft, *, expected_revision: int) -> CommitResult:
        return self.commit(DurableMutation(draft.conversation_id, expected_revision, entries=(draft,)))

    def submit(self, draft: SubmissionDraft) -> CommitResult:
        _head, revision = self.head(draft.conversation_id)
        entries = () if draft.entry is None else (draft.entry,)
        return self.commit(DurableMutation(draft.conversation_id, revision, entries=entries, submission=draft))

    def commit(self, mutation: DurableMutation) -> CommitResult:
        with closing(self._connect()) as database:
            database.execute("BEGIN IMMEDIATE")
            try:
                existing_submission = _find_submission(database, mutation.submission)
                if existing_submission is not None:
                    return existing_submission
                creation = mutation.create_conversation
                if creation is not None:
                    if database.execute("SELECT 1 FROM conversations WHERE id=?", (mutation.conversation_id,)).fetchone():
                        raise CommitConflict("conversation already exists")
                    if creation.parent_conversation_id is not None:
                        context_path(
                            creation.parent_conversation_id, creation.parent_entry_id,
                            entries=lambda scope: _entries(database, scope),
                            parent=lambda scope: _conversation_parent(database, scope),
                        )
                    database.execute(
                        "INSERT INTO conversations(id, head_entry_id, revision, parent_conversation_id, parent_entry_id) "
                        "VALUES(?, NULL, 0, ?, ?)",
                        (mutation.conversation_id, creation.parent_conversation_id, creation.parent_entry_id),
                    )
                current_revision, head = _read_conversation(database, mutation.conversation_id)
                if mutation.delete_conversation is not None:
                    child = database.execute(
                        "SELECT 1 FROM conversations WHERE parent_conversation_id=? LIMIT 1",
                        (mutation.conversation_id,),
                    ).fetchone()
                    if child is not None:
                        raise CommitConflict("conversation has forked children")
                    if current_revision != mutation.expected_revision:
                        raise CommitConflict("conversation revision changed")
                    record = _new_commit(database, "durable", {
                        **mutation_projection(mutation),
                    })
                    task_ids = [
                        str(row[0]) for row in database.execute(
                            "SELECT id FROM tasks WHERE conversation_id=?",
                            (mutation.conversation_id,),
                        ).fetchall()
                    ]
                    if task_ids:
                        placeholders = ",".join("?" for _ in task_ids)
                        database.execute(f"DELETE FROM effects WHERE task_id IN ({placeholders})", task_ids)
                    database.execute("DELETE FROM tasks WHERE conversation_id=?", (mutation.conversation_id,))
                    database.execute("DELETE FROM submissions WHERE conversation_id=?", (mutation.conversation_id,))
                    database.execute("DELETE FROM entries WHERE conversation_id=?", (mutation.conversation_id,))
                    database.execute("DELETE FROM conversations WHERE id=?", (mutation.conversation_id,))
                    database.execute("COMMIT")
                    return CommitResult(current_revision + 1, record.sequence, record=record)
                _validate_mutation(database, mutation, current_revision, head)
                if mutation.submission is not None and database.execute(
                    "SELECT 1 FROM submissions WHERE id=?",
                    (mutation.submission.submission_id,),
                ).fetchone() is not None:
                    raise IdempotencyConflict("submission ID was already used")
                new_head = mutation.branch_entry_id or (mutation.entries[-1].entry_id if mutation.entries else head)
                record = _new_commit(database, "durable", {
                    **mutation_projection(mutation), "_workflow": execution_payload(mutation, new_head),
                })
                sequence = record.sequence
                _write_entries(database, mutation, current_revision, sequence)
                new_revision = current_revision + (1 if mutation.branch_entry_id is not None else len(mutation.entries))
                if mutation.entries or mutation.branch_entry_id is not None:
                    database.execute(
                        "UPDATE conversations SET head_entry_id=?, revision=?, updated_at=? WHERE id=?",
                        (new_head, new_revision, datetime.now(UTC).isoformat(), mutation.conversation_id),
                    )
                elif (
                    mutation.task is not None
                    or mutation.task_updates
                    or mutation.effect is not None
                    or mutation.submission_update is not None
                    or mutation.submission_updates
                ):
                    database.execute(
                        "UPDATE conversations SET updated_at=? WHERE id=?",
                        (datetime.now(UTC).isoformat(), mutation.conversation_id),
                    )
                if mutation.submission is not None:
                    database.execute(
                        "INSERT INTO submissions(id, conversation_id, request_id, entry_id, revision, fingerprint, status, placement, mode, content, reason, commit_seq) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            mutation.submission.submission_id,
                            mutation.submission.conversation_id,
                            mutation.submission.request_id,
                            None if mutation.submission.entry is None else mutation.submission.entry.entry_id,
                            new_revision,
                            mutation.submission.fingerprint,
                            SubmissionStatus(mutation.submission.status).value,
                            SubmissionPlacement(mutation.submission.placement).value,
                            mutation.submission.mode,
                            _encode(mutation.submission.content or {}),
                            mutation.submission.reason,
                            sequence,
                        ),
                    )
                _write_task(database, mutation.task)
                _write_tasks(database, mutation.task_updates)
                _write_effect(database, mutation.effect, mutation.expected_effect_status)
                _write_effects(database, mutation.effects)
                for settlement in mutation.settlements:
                    _write_effect(database, settlement, "pending")
                _write_artifacts(database, mutation.artifacts)
                _write_submission_update(database, mutation.submission_update, mutation.entries)
                _write_submission_updates(database, mutation.submission_updates, mutation.entries)
                database.execute("COMMIT")
                return CommitResult(
                    revision=new_revision,
                    sequence=sequence,
                    submission_id=(
                        mutation.submission.submission_id
                        if mutation.submission is not None
                        else (
                            mutation.submission_update.submission_id
                            if mutation.submission_update is not None
                            else mutation.submission_updates[-1].submission_id
                            if mutation.submission_updates
                            else None
                        )
                    ),
                    record=record,
                )
            except BaseException:
                if database.in_transaction:
                    database.execute("ROLLBACK")
                raise

    def entries(self, conversation_id: str) -> tuple[EntryDraft, ...]:
        with closing(self._connect()) as database:
            return _entries(database, conversation_id)

    def head(self, conversation_id: str) -> tuple[str | None, int]:
        with closing(self._connect()) as database:
            row = database.execute("SELECT head_entry_id, revision FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown conversation: {conversation_id}")
            return row["head_entry_id"], int(row["revision"])

    def branch(
        self, conversation_id: str, entry_id: str, *, expected_revision: int | None = None,
    ) -> CommitResult:
        _head, revision = self.head(conversation_id)
        return self.commit(DurableMutation(
            conversation_id=conversation_id,
            expected_revision=revision if expected_revision is None else expected_revision,
            branch_entry_id=entry_id,
        ))

    def fork(self, conversation_id: str, entry_id: str, child_conversation_id: str) -> CommitResult:
        with closing(self._connect()) as database:
            database.execute("BEGIN IMMEDIATE")
            try:
                context_path(
                    conversation_id, entry_id,
                    entries=lambda scope: _entries(database, scope),
                    parent=lambda scope: _conversation_parent(database, scope),
                )
                database.execute(
                    "INSERT INTO conversations(id, head_entry_id, revision, parent_conversation_id, parent_entry_id) VALUES(?, NULL, 0, ?, ?)",
                    (child_conversation_id, conversation_id, entry_id),
                )
                record = _new_commit(database, "fork", {
                    "session_id": child_conversation_id, "parent_session_id": conversation_id, "parent_entry_id": entry_id,
                })
                database.execute("COMMIT")
                return CommitResult(0, record.sequence, record=record)
            except BaseException:
                if database.in_transaction:
                    database.execute("ROLLBACK")
                raise

    def delete_conversation(self, conversation_id: str) -> None:
        with closing(self._connect()) as database:
            database.execute("BEGIN IMMEDIATE")
            try:
                if database.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone() is None:
                    raise KeyError(f"unknown conversation: {conversation_id}")
                if database.execute(
                    "SELECT 1 FROM conversations WHERE parent_conversation_id=? LIMIT 1",
                    (conversation_id,),
                ).fetchone() is not None:
                    raise CommitConflict("conversation has forked children")
                task_rows = database.execute(
                    "SELECT id FROM tasks WHERE conversation_id=?", (conversation_id,)
                ).fetchall()
                task_ids = [str(row[0]) for row in task_rows]
                if task_ids:
                    database.executemany("DELETE FROM effects WHERE task_id=?", ((task_id,) for task_id in task_ids))
                    database.executemany("DELETE FROM artifacts WHERE owner_task_id=?", ((task_id,) for task_id in task_ids))
                database.execute("DELETE FROM tasks WHERE conversation_id=?", (conversation_id,))
                database.execute("DELETE FROM submissions WHERE conversation_id=?", (conversation_id,))
                database.execute("DELETE FROM entries WHERE conversation_id=?", (conversation_id,))
                database.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
                database.execute("COMMIT")
            except BaseException:
                if database.in_transaction:
                    database.execute("ROLLBACK")
                raise

    def conversation_parent(self, conversation_id: str) -> tuple[str | None, str | None]:
        with closing(self._connect()) as database:
            return _conversation_parent(database, conversation_id)

    def has_conversation(self, conversation_id: str) -> bool:
        with closing(self._connect()) as database:
            row = database.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            return bool(row)

    def task(self, task_id: str) -> TaskCheckpoint:
        with closing(self._connect()) as database:
            row = database.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown task: {task_id}")
            return task_from_row(row)

    def effect(self, effect_id: str) -> EffectRecord:
        with closing(self._connect()) as database:
            row = database.execute("SELECT * FROM effects WHERE id=?", (effect_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown effect: {effect_id}")
            return effect_from_row(row)

    def approval(self, approval_id: str) -> ApprovalRecord:
        with closing(self._connect()) as database:
            rows = database.execute("SELECT state FROM tasks").fetchall()
            for row in rows:
                state = json.loads(row["state"])
                approvals: dict[str, object] = (
                    state.get("approvals", {}) if isinstance(state, dict) else {}
                )
                payload = approvals.get(approval_id) if isinstance(approvals, dict) else None
                if isinstance(payload, dict):
                    return ApprovalRecord.from_payload(payload)
            raise KeyError(f"unknown approval: {approval_id}")

    def effects_for_task(self, task_id: str) -> tuple[EffectRecord, ...]:
        with closing(self._connect()) as database:
            rows = database.execute(
                "SELECT id, task_id, category, replay_class, args_digest, status, intent, settlement "
                "FROM effects WHERE task_id=? ORDER BY rowid",
                (task_id,),
            ).fetchall()
            return tuple(effect_from_row(row) for row in rows)

    def artifact(self, handle: str) -> ArtifactRecord:
        with closing(self._connect()) as database:
            row = database.execute("SELECT * FROM artifacts WHERE handle=?", (handle,)).fetchone()
            if row is None:
                raise KeyError(f"unknown artifact: {handle}")
            return artifact_from_row(row)

    def artifacts_for_task(self, task_id: str) -> tuple[ArtifactRecord, ...]:
        with closing(self._connect()) as database:
            rows = database.execute("SELECT * FROM artifacts WHERE owner_task_id=? ORDER BY handle", (task_id,)).fetchall()
            return tuple(artifact_from_row(row) for row in rows)

    def artifacts_for_effect(self, effect_id: str) -> tuple[ArtifactRecord, ...]:
        with closing(self._connect()) as database:
            rows = database.execute("SELECT * FROM artifacts WHERE owner_effect_id=? ORDER BY handle", (effect_id,)).fetchall()
            return tuple(artifact_from_row(row) for row in rows)

    def submission(self, submission_id: str) -> SubmissionRecord:
        with closing(self._connect()) as database:
            row = database.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown submission: {submission_id}")
            return submission_from_row(row)

    def queued_submissions(self, conversation_id: str | None = None) -> tuple[SubmissionRecord, ...]:
        with closing(self._connect()) as database:
            if conversation_id is None:
                rows = database.execute(
                    "SELECT * FROM submissions WHERE status=? AND placement=? ORDER BY commit_seq, id",
                    (SubmissionStatus.QUEUED.value, SubmissionPlacement.QUEUED.value),
                ).fetchall()
            else:
                rows = database.execute(
                    "SELECT * FROM submissions WHERE conversation_id=? AND status=? AND placement=? "
                    "ORDER BY commit_seq, id",
                    (conversation_id, SubmissionStatus.QUEUED.value, SubmissionPlacement.QUEUED.value),
                ).fetchall()
            return tuple(submission_from_row(row) for row in rows)

    def submission_result_for_request(
        self, conversation_id: str, request_id: str, fingerprint: str,
    ) -> CommitResult | None:
        with closing(self._connect()) as database:
            row = database.execute(
                "SELECT id, fingerprint, revision, commit_seq FROM submissions WHERE conversation_id=? AND request_id=?",
                (conversation_id, request_id),
            ).fetchone()
            if row is None:
                return None
            if str(row["fingerprint"]) != fingerprint:
                raise IdempotencyConflict("request_id was already admitted with different durable result content")
            return CommitResult(int(row["revision"]), int(row["commit_seq"]), str(row["id"]))

    def pending_tasks(self) -> tuple[TaskCheckpoint, ...]:
        with closing(self._connect()) as database:
            rows = database.execute(
                "SELECT * FROM tasks WHERE status NOT IN ('completed','failed','cancelled') ORDER BY rowid"
            ).fetchall()
            return tuple(task_from_row(row) for row in rows)

    def finalization_queue(self) -> tuple[str, ...]:
        with closing(self._connect()) as database:
            rows = database.execute(
                "SELECT task_id FROM finalization_queue ORDER BY task_id"
            ).fetchall()
            return tuple(str(row["task_id"]) for row in rows)

    def tasks(self) -> tuple[TaskCheckpoint, ...]:
        with closing(self._connect()) as database:
            rows = database.execute("SELECT * FROM tasks ORDER BY rowid").fetchall()
            return tuple(task_from_row(row) for row in rows)

    def waiting_dependents(self, task_id: str) -> tuple[TaskCheckpoint, ...]:
        with closing(self._connect()) as database:
            rows = database.execute(
                "SELECT tasks.* FROM task_waiters "
                "JOIN tasks ON tasks.id=task_waiters.waiter_task_id "
                "WHERE task_waiters.waited_task_id=? ORDER BY tasks.rowid",
                (task_id,),
            ).fetchall()
            return tuple(task_from_row(row) for row in rows)

    def owned_dependents(self, task_id: str) -> tuple[TaskCheckpoint, ...]:
        with closing(self._connect()) as database:
            rows = database.execute(
                "SELECT tasks.* FROM task_owners "
                "JOIN tasks ON tasks.id=task_owners.child_task_id "
                "WHERE task_owners.owner_task_id=? ORDER BY tasks.rowid",
                (task_id,),
            ).fetchall()
            return tuple(task_from_row(row) for row in rows)

    def acquire_scheduler_owner(self, owner_id: str, process_id: int) -> None:
        if not owner_id or type(process_id) is not int or process_id <= 0:
            raise ValueError("scheduler owner identity is invalid")
        with closing(self._connect()) as database:
            database.execute("BEGIN IMMEDIATE")
            try:
                row = database.execute(
                    "SELECT owner_id, process_id FROM scheduler_owners WHERE id=1"
                ).fetchone()
                now = datetime.now(UTC).isoformat()
                if row is None:
                    database.execute(
                        "INSERT INTO scheduler_owners(id, owner_id, process_id, claimed_at) VALUES(1, ?, ?, ?)",
                        (owner_id, process_id, now),
                    )
                elif str(row["owner_id"]) == owner_id:
                    database.execute(
                        "UPDATE scheduler_owners SET process_id=?, claimed_at=? WHERE id=1",
                        (process_id, now),
                    )
                else:
                    old_pid = int(row["process_id"])
                    try:
                        os.kill(old_pid, 0)
                        alive = True
                    except OSError:
                        alive = False
                    if alive:
                        raise SchedulerOwnershipError(
                            "another live scheduler owns this durable database"
                        )
                    database.execute(
                        "UPDATE scheduler_owners SET owner_id=?, process_id=?, claimed_at=? WHERE id=1",
                        (owner_id, process_id, now),
                    )
                database.execute("COMMIT")
            except BaseException:
                if database.in_transaction:
                    database.execute("ROLLBACK")
                raise

    def release_scheduler_owner(self, owner_id: str) -> None:
        with closing(self._connect()) as database:
            database.execute(
                "DELETE FROM scheduler_owners WHERE id=1 AND owner_id=?",
                (owner_id,),
            )

    def close(self) -> None:
        return None

    def commit_sequence(self) -> int:
        with closing(self._connect()) as database:
            return int(database.execute("SELECT COALESCE(MAX(seq), 0) FROM commits").fetchone()[0])

    def commits_after(self, sequence: int, *, limit: int = 512) -> tuple[CommitRecord, ...]:
        bounded = max(1, min(2048, int(limit)))
        with closing(self._connect()) as database:
            rows = database.execute(
                "SELECT seq, kind, committed_at, payload FROM commits WHERE seq>? ORDER BY seq LIMIT ?",
                (int(sequence), bounded),
            ).fetchall()
            return tuple(
                CommitRecord(int(row["seq"]), str(row["kind"]), str(row["committed_at"]),
                             public_commit_payload(json.loads(row["payload"])))
                for row in rows
            )

    def task_history(self, task_id: str, *, through: int | None = None) -> tuple[TaskTransition, ...]:
        with closing(self._connect()) as database:
            rows = database.execute(
                "SELECT seq, json_extract(payload, '$._workflow') AS workflow FROM commits "
                "WHERE json_extract(payload, '$._workflow') IS NOT NULL "
                "AND (? IS NULL OR seq<=?) ORDER BY seq", (through, through),
            ).fetchall()
            history: list[TaskTransition] = []
            for row in rows:
                raw = json.loads(row["workflow"])
                task_rows = raw.get("tasks")
                matches = (
                    isinstance(task_rows, list)
                    and any(isinstance(item, dict) and item.get("task_id") == task_id for item in task_rows)
                )
                legacy_task = raw.get("task")
                legacy_match = isinstance(legacy_task, dict) and legacy_task.get("task_id") == task_id
                if matches or legacy_match:
                    history.append(transition_from_payload(int(row["seq"]), raw, task_id))
            return tuple(history)


def _entries(database: sqlite3.Connection, conversation_id: str) -> tuple[EntryDraft, ...]:
    rows = database.execute(
        "SELECT id, conversation_id, parent_id, kind, payload, commit_seq, by_task_id "
        "FROM entries WHERE conversation_id=? ORDER BY ordinal", (conversation_id,),
    ).fetchall()
    if not rows and database.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone() is None:
        raise KeyError(f"unknown conversation: {conversation_id}")
    return tuple(entry_from_row(row) for row in rows)


def _conversation_parent(database: sqlite3.Connection, conversation_id: str) -> tuple[str | None, str | None]:
    row = database.execute(
        "SELECT parent_conversation_id, parent_entry_id FROM conversations WHERE id=?", (conversation_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown conversation: {conversation_id}")
    return row["parent_conversation_id"], row["parent_entry_id"]


def _encode(payload: object) -> str:
    return json.dumps(thaw_json(payload), ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def _same_entry(row: sqlite3.Row, entry: EntryDraft) -> bool:
    return (
        row["parent_id"] == entry.parent_id
        and str(row["kind"]) == entry.kind
        and str(row["payload"]) == _encode(entry.payload)
    )


def _find_submission(database: sqlite3.Connection, draft: SubmissionDraft | None) -> CommitResult | None:
    if draft is None:
        return None
    row = database.execute(
        "SELECT s.id, s.fingerprint, s.revision, s.commit_seq, s.placement, s.mode, s.content, "
        "e.id AS entry_id, e.parent_id, e.kind, e.payload "
        "FROM submissions s LEFT JOIN entries e ON e.id=s.entry_id "
        "WHERE s.conversation_id=? AND s.request_id=?",
        (draft.conversation_id, draft.request_id),
    ).fetchone()
    if row is None:
        return None
    stored_entry = None
    if row["entry_id"] is not None:
        stored_entry = EntryDraft(str(row["entry_id"]), draft.conversation_id, row["parent_id"], str(row["kind"]), json.loads(row["payload"]))
    stored = SubmissionDraft(
        str(row["id"]), draft.request_id, draft.conversation_id, stored_entry,
        str(row["fingerprint"]), placement=str(row["placement"]), mode=str(row["mode"]),
        content=json.loads(str(row["content"])),
    )
    if not same_submission(stored, draft):
        raise IdempotencyConflict("request_id was already admitted with different input")
    return CommitResult(int(row["revision"]), int(row["commit_seq"]), str(row["id"]))


def _new_commit(database: sqlite3.Connection, kind: str, payload: Mapping[str, object]) -> CommitRecord:
    committed_at = datetime.now(UTC).isoformat()
    identity = database.execute("SELECT value FROM schema_metadata WHERE key='database_id'").fetchone()
    if identity is None:
        raise SchemaVersionError("durable database identity is missing")
    payload = {**payload, "database_id": str(identity["value"])}
    cursor = database.execute(
        "INSERT INTO commits(kind, committed_at, payload) VALUES(?, ?, ?)",
        (kind, committed_at, _encode(payload)),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite did not return a commit sequence")
    return CommitRecord(int(cursor.lastrowid), kind, committed_at, public_commit_payload(payload))


def _read_conversation(database: sqlite3.Connection, conversation_id: str) -> tuple[int, str | None]:
    row = database.execute("SELECT head_entry_id, revision FROM conversations WHERE id=?", (conversation_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown conversation: {conversation_id}")
    return int(row["revision"]), row["head_entry_id"]


def _validate_mutation(
    database: sqlite3.Connection,
    mutation: DurableMutation,
    revision: int,
    head: str | None,
) -> None:
    if revision != mutation.expected_revision:
        raise CommitConflict("conversation revision changed")
    if (
        mutation.branch_entry_id is not None
        and database.execute(
            "SELECT 1 FROM entries WHERE conversation_id=? AND id=?",
            (mutation.conversation_id, mutation.branch_entry_id),
        ).fetchone() is None
    ):
        raise CommitConflict("branch entry does not belong to conversation")
    parent = head
    for entry in mutation.entries:
        if entry.parent_id != parent:
            raise CommitConflict("mutation entries do not extend the active branch")
        if database.execute("SELECT 1 FROM entries WHERE id=?", (entry.entry_id,)).fetchone() is not None:
            raise CommitConflict("entry ID already exists")
        parent = entry.entry_id
    _validate_task(database, mutation.task, mutation.submission, mutation.expected_attempt_token)
    for task in mutation.task_updates:
        _validate_task(database, task)
    _validate_effect(database, mutation)
    _validate_artifacts(database, mutation)
    _validate_submission_update(database, mutation.submission_update, mutation.entries)
    for update in mutation.submission_updates:
        _validate_submission_update(database, update, mutation.entries)


def _validate_task(
    database: sqlite3.Connection,
    task: TaskCheckpoint | None,
    proposed_submission: SubmissionDraft | None = None,
    expected_attempt_token: str | None = None,
) -> None:
    if task is None:
        return
    submission = database.execute(
        "SELECT conversation_id FROM submissions WHERE id=?",
        (task.submission_id,),
    ).fetchone()
    proposed_matches = (
        proposed_submission is not None
        and proposed_submission.submission_id == task.submission_id
        and proposed_submission.conversation_id == task.conversation_id
    )
    if (submission is None and not proposed_matches) or (
        submission is not None and str(submission["conversation_id"]) != task.conversation_id
    ):
        raise CommitConflict("task must belong to a committed submission")
    row = database.execute(
        "SELECT conversation_id, submission_id, revision, provider_snapshot, phase, status, state FROM tasks WHERE id=?",
        (task.task_id,),
    ).fetchone()
    if row is None and task.revision != 0:
        raise CommitConflict("new task must start at revision zero")
    if row is None and not valid_task_transition(None, task.phase):
        raise CommitConflict("new task must start in accepted or dispatching phase")
    owner_task_id = task.owner_task_id
    _validate_task_owner(database, task, owner_task_id)
    if row is not None:
        _validate_existing_task(row, task, expected_attempt_token)
    elif not valid_task_status_transition(None, TaskStatus(str(task.status))):
        raise CommitConflict("new task must start with a non-terminal scheduler status")


def _validate_task_owner(
    database: sqlite3.Connection,
    task: TaskCheckpoint,
    owner_task_id: str | None,
) -> None:
    if owner_task_id is None:
        return
    if owner_task_id == task.task_id:
        raise CommitConflict("a task cannot own itself")
    owner = database.execute("SELECT id FROM tasks WHERE id=?", (owner_task_id,)).fetchone()
    if owner is None:
        raise CommitConflict("owner task does not exist")
    seen: set[str] = {task.task_id}
    cursor = owner_task_id
    while cursor not in seen:
        seen.add(cursor)
        parent = database.execute("SELECT state FROM tasks WHERE id=?", (cursor,)).fetchone()
        if parent is None:
            break
        raw = json.loads(parent["state"])
        next_owner = raw.get("owner_task_id") if isinstance(raw, dict) else None
        if not isinstance(next_owner, str) or not next_owner:
            break
        cursor = next_owner
    if cursor in seen and cursor == task.task_id:
        raise CommitConflict("task ownership cycle")


def _validate_existing_task(
    row: sqlite3.Row,
    task: TaskCheckpoint,
    expected_attempt_token: str | None,
) -> None:
    previous_state = json.loads(row["state"])
    if previous_state.get("cancel_requested") and task.phase not in {"cancelled", "failed"}:
        raise CommitConflict("task cancellation barrier is active")
    if not valid_execution_attempt(previous_state, TaskStatus(str(row["status"])), task, expected_attempt_token):
        raise CommitConflict("task execution attempt changed")
    if task.revision != int(row["revision"]) + 1:
        raise CommitConflict("task revision changed")
    if (
        str(row["conversation_id"]) != task.conversation_id
        or str(row["submission_id"]) != task.submission_id
        or str(row["provider_snapshot"]) != _encode(task.provider_snapshot)
    ):
        raise CommitConflict("task identity is immutable")
    if not valid_frozen_task_state(json.loads(row["state"]), task.state):
        raise CommitConflict("admitted task state is immutable")
    previous_phase = str(row["phase"])
    if not valid_task_transition(previous_phase, task.phase):
        raise CommitConflict(f"invalid task phase transition: {previous_phase} -> {task.phase}")
    previous_status = TaskStatus(str(row["status"]))
    if not valid_task_status_transition(previous_status, TaskStatus(str(task.status))):
        raise CommitConflict(f"invalid task status transition: {previous_status} -> {task.status}")


def _validate_effect(database: sqlite3.Connection, mutation: DurableMutation) -> None:
    effects = ((mutation.effect,) if mutation.effect is not None else ()) + mutation.effects + mutation.settlements
    if not effects:
        return
    if mutation.task is None:
        raise CommitConflict("effect mutation requires its task checkpoint")
    for effect in effects:
        task_exists = database.execute("SELECT 1 FROM tasks WHERE id=?", (effect.task_id,)).fetchone() is not None
        task_is_new = mutation.task is not None and mutation.task.task_id == effect.task_id
        if not task_exists and not task_is_new:
            raise CommitConflict("effect task does not exist")
        row = database.execute(
            "SELECT task_id, category, replay_class, args_digest, status, intent, settlement FROM effects WHERE id=?",
            (effect.effect_id,),
        ).fetchone()
        if row is None:
            if (effect is mutation.effect and mutation.expected_effect_status is not None) or effect in mutation.settlements:
                raise CommitConflict("effect status changed")
            continue
        expected = "pending" if effect in mutation.settlements else mutation.expected_effect_status
        if row["settlement"] is not None:
            raise CommitConflict("effect is already settled")
        if (effect is not mutation.effect and effect not in mutation.settlements) or row["status"] != expected:
            raise CommitConflict("effect status changed")
        original = (str(row["task_id"]), str(row["category"]), str(row["replay_class"]), str(row["args_digest"]), str(row["intent"]))
        candidate = (effect.task_id, effect.category, effect.replay_class, effect.args_digest, _encode(effect.intent))
        if original != candidate:
            raise CommitConflict("effect identity is immutable")


def _validate_artifacts(database: sqlite3.Connection, mutation: DurableMutation) -> None:
    proposed_effects = {
        effect.effect_id
        for effect in ((mutation.effect,) if mutation.effect is not None else ())
        + mutation.effects + mutation.settlements
    }
    proposed_tasks = {
        task.task_id for task in ((mutation.task,) if mutation.task is not None else ()) + mutation.task_updates
    }
    for artifact in mutation.artifacts:
        if database.execute("SELECT 1 FROM artifacts WHERE handle=?", (artifact.handle,)).fetchone() is not None:
            raise CommitConflict("artifact handle already exists")
        if artifact.owner_task_id is not None:
            task_exists = database.execute("SELECT 1 FROM tasks WHERE id=?", (artifact.owner_task_id,)).fetchone() is not None
            if not task_exists and artifact.owner_task_id not in proposed_tasks:
                raise CommitConflict("artifact owner task does not exist")
        if artifact.owner_effect_id is not None:
            effect_exists = database.execute("SELECT 1 FROM effects WHERE id=?", (artifact.owner_effect_id,)).fetchone() is not None
            if not effect_exists and artifact.owner_effect_id not in proposed_effects:
                raise CommitConflict("artifact owner effect does not exist")


def _validate_submission_update(
    database: sqlite3.Connection,
    update: SubmissionUpdate | None,
    entries: tuple[EntryDraft, ...],
) -> None:
    if update is None:
        return
    row = database.execute(
        "SELECT conversation_id, status, placement, entry_id AS input_entry_id FROM submissions WHERE id=?",
        (update.submission_id,),
    ).fetchone()
    if row is None or str(row["conversation_id"]) != update.conversation_id:
        raise CommitConflict("unknown submission")
    current = SubmissionStatus(str(row["status"]))
    if current != update.expected_status or (
        current != update.status and not valid_submission_transition(current, update.status)
    ):
        raise CommitConflict("submission status changed")
    if update.answer_entry_id is not None and update.answer_entry_id not in {entry.entry_id for entry in entries}:
        raise CommitConflict("answer entry must be part of the same commit")
    if update.input_entry_id is not None and update.input_entry_id not in {entry.entry_id for entry in entries}:
        raise CommitConflict("placed input entry must be part of the same commit")
    if update.placement is not None:
        placement = SubmissionPlacement(update.placement)
        current_placement = SubmissionPlacement(str(row["placement"]))
        if current_placement is SubmissionPlacement.PLACED and placement is not SubmissionPlacement.PLACED:
            raise CommitConflict("placed submission cannot return to queued")
        if placement is SubmissionPlacement.PLACED and update.input_entry_id is None and row["input_entry_id"] is None:
            raise CommitConflict("placing a submission requires an input entry")


def _write_entries(database: sqlite3.Connection, mutation: DurableMutation, revision: int, sequence: int) -> None:
    task_by_submission = {
        task.submission_id: task.task_id
        for task in ((mutation.task,) if mutation.task is not None else ()) + mutation.task_updates
    }
    entry_task_ids = {
        update.input_entry_id: task_by_submission[update.submission_id]
        for update in (
            ((mutation.submission_update,) if mutation.submission_update is not None else ())
            + mutation.submission_updates
        )
        if update.input_entry_id is not None and update.submission_id in task_by_submission
    }
    for ordinal, entry in enumerate(mutation.entries, start=revision + 1):
        database.execute(
            "INSERT INTO entries(id, conversation_id, parent_id, kind, payload, ordinal, commit_seq, by_task_id) VALUES(?,?,?,?,?,?,?,?)",
            (entry.entry_id, entry.conversation_id, entry.parent_id, entry.kind, _encode(entry.payload), ordinal, sequence,
             entry.by_task_id or entry_task_ids.get(entry.entry_id)),
        )


def _write_task(database: sqlite3.Connection, task: TaskCheckpoint | None) -> None:
    if task is None:
        return
    database.execute(
        "INSERT INTO tasks(id, conversation_id, submission_id, phase, revision, provider_snapshot, state, current_turn_id, pending_effect_id, status, schema_version) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET phase=excluded.phase, revision=excluded.revision, "
        "provider_snapshot=excluded.provider_snapshot, state=excluded.state, current_turn_id=excluded.current_turn_id, "
        "pending_effect_id=excluded.pending_effect_id, status=excluded.status, schema_version=excluded.schema_version",
        (task.task_id, task.conversation_id, task.submission_id, task.phase, task.revision, _encode(task.provider_snapshot), _encode(task.state),
         task.current_turn_id, task.pending_effect_id, TaskStatus(str(task.status)).value, task.schema_version),
    )
    database.execute("DELETE FROM task_waiters WHERE waiter_task_id=?", (task.task_id,))
    database.executemany(
        "INSERT INTO task_waiters(waited_task_id, waiter_task_id) VALUES(?, ?)",
        ((dependency, task.task_id) for dependency in task.waiting_on),
    )
    database.execute("DELETE FROM task_owners WHERE child_task_id=?", (task.task_id,))
    if task.owner_task_id is not None:
        database.execute(
            "INSERT INTO task_owners(owner_task_id, child_task_id) VALUES(?, ?)",
            (task.owner_task_id, task.task_id),
        )
    if TaskStatus(str(task.status)) in {TaskStatus.COMPLETING, TaskStatus.WAITING}:
        database.execute(
            "INSERT OR IGNORE INTO finalization_queue(task_id) VALUES(?)",
            (task.task_id,),
        )
    else:
        database.execute("DELETE FROM finalization_queue WHERE task_id=?", (task.task_id,))


def _write_tasks(database: sqlite3.Connection, tasks: tuple[TaskCheckpoint, ...]) -> None:
    for task in tasks:
        _write_task(database, task)


def _write_effect(database: sqlite3.Connection, effect: EffectRecord | None, expected_status: str | None) -> None:
    if effect is None:
        return
    settlement = None if effect.settlement is None else _encode(effect.settlement)
    if expected_status is None:
        database.execute(
            "INSERT INTO effects(id, task_id, category, replay_class, args_digest, status, intent, settlement) VALUES(?,?,?,?,?,?,?,?)",
            (effect.effect_id, effect.task_id, effect.category, effect.replay_class, effect.args_digest, effect.status, _encode(effect.intent), settlement),
        )
        return
    cursor = database.execute(
        "UPDATE effects SET status=?, settlement=? WHERE id=? AND status=?",
        (effect.status, settlement, effect.effect_id, expected_status),
    )
    if cursor.rowcount != 1:
        raise CommitConflict("effect status changed")


def _write_effects(database: sqlite3.Connection, effects: tuple[EffectRecord, ...]) -> None:
    for effect in effects:
        database.execute(
            "INSERT INTO effects(id, task_id, category, replay_class, args_digest, status, intent, settlement) VALUES(?,?,?,?,?,?,?,?)",
            (effect.effect_id, effect.task_id, effect.category, effect.replay_class, effect.args_digest, effect.status, _encode(effect.intent),
             None if effect.settlement is None else _encode(effect.settlement)),
        )


def _write_artifacts(database: sqlite3.Connection, artifacts: tuple[ArtifactRecord, ...]) -> None:
    for artifact in artifacts:
        database.execute(
            "INSERT INTO artifacts(handle, sha256, byte_count, storage_path, owner_task_id, owner_effect_id, created_at, metadata) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (artifact.handle, artifact.sha256, artifact.byte_count, artifact.storage_path,
             artifact.owner_task_id, artifact.owner_effect_id, artifact.created_at, _encode(artifact.metadata)),
        )


def _write_submission_update(
    database: sqlite3.Connection,
    update: SubmissionUpdate | None,
    entries: tuple[EntryDraft, ...] = (),
) -> None:
    if update is None:
        return
    cursor = database.execute(
        "UPDATE submissions SET status=?, answer_entry_id=?, reason=?, detail=?, entry_id=COALESCE(?, entry_id), placement=COALESCE(?, placement) "
        "WHERE id=? AND conversation_id=? AND status=?",
        (
            update.status.value,
            update.answer_entry_id,
            update.reason,
            None if update.detail is None else _encode(update.detail),
            update.input_entry_id,
            None if update.placement is None else SubmissionPlacement(update.placement).value,
            update.submission_id,
            update.conversation_id,
            update.expected_status.value,
        ),
    )
    if cursor.rowcount != 1:
        raise CommitConflict("submission status changed")


def _write_submission_updates(
    database: sqlite3.Connection,
    updates: tuple[SubmissionUpdate, ...],
    entries: tuple[EntryDraft, ...] = (),
) -> None:
    for update in updates:
        _write_submission_update(database, update, entries)
