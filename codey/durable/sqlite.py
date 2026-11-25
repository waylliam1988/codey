from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import AbstractContextManager, closing, contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path

from codey.durable.ancestry import context_path
from codey.durable.entry_ownership import finalized_entries
from codey.durable.errors import (
    CommitConflict,
    IdempotencyConflict,
    SchedulerOwnershipError,
    SchemaVersionError,
)
from codey.durable.events import mutation_projection
from codey.durable.history import HistoryReader
from codey.durable.history_index import rewrites_path
from codey.durable.history_lifetime import database_lifetime
from codey.durable.model import (
    ApprovalRecord,
    ArtifactRecord,
    CommitRecord,
    CommitResult,
    DeleteConversation,
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
    migrate_checkpoint_phase,
    same_submission,
    thaw_json,
    valid_cancellation_cleanup,
    valid_execution_attempt,
    valid_frozen_task_state,
    valid_input_consumption,
    valid_submission_transition,
    valid_task_status_transition,
    valid_task_transition,
)
from codey.durable.sqlite_display import write_display_index
from codey.durable.sqlite_history import SQLiteHistoryReader
from codey.durable.sqlite_records import (
    _column,
    _integer,
    _object,
    _optional_object,
    _optional_text,
    _text,
    artifact_from_row,
    effect_from_row,
    entry_from_row,
    submission_from_row,
    task_from_row,
)
from codey.durable.sqlite_schema import initialize_database
from codey.durable.store import validate_submission_update_state
from codey.durable.timeline import (
    TaskSettlementHistory,
    TaskTransition,
    execution_payload,
    public_commit_payload,
    settlement_histories,
    transition_from_payload,
)
from codey.storage.file_lock import acquire_lease
from codey.utils.object_fields import object_fields

_ROW_FACTORY: Callable[[sqlite3.Cursor, tuple[object, ...]], sqlite3.Row] = sqlite3.Row


def _exists(database: sqlite3.Connection, sql: str, parameters: tuple[str, ...]) -> bool:
    row: sqlite3.Row | None = database.execute(sql, parameters).fetchone()
    return row is not None


class SQLiteDurableStore:
    """SQLite implementation of the durable append contract."""

    def __init__(self, path: str | Path, *, synchronous: str = "FULL") -> None:
        if synchronous not in {"FULL", "NORMAL"}:
            raise ValueError("synchronous must be 'FULL' or 'NORMAL'")
        self.path = Path(path)
        self._synchronous = synchronous
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.history_lifetime = database_lifetime(self.path)
        # WAL mode changes precede BEGIN IMMEDIATE and do not reliably invoke
        # SQLite's busy handler. Serialize that complete cold-start boundary.
        with acquire_lease(self.path.with_name(self.path.name + ".initialization"), timeout_seconds=30):
            self._database_id = initialize_database(self._connect)

    def _connect(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        database.row_factory = _ROW_FACTORY
        database.execute(f"PRAGMA synchronous={self._synchronous}")
        database.execute("PRAGMA foreign_keys=ON")
        return database

    @property
    def database_id(self) -> str:
        return self._database_id

    def create_conversation(self, conversation_id: str, *, parent_conversation_id: str | None = None, parent_entry_id: str | None = None) -> None:
        if not conversation_id:
            raise ValueError("conversation_id is required")
        with closing(self._connect()) as database, database:
            database.execute("BEGIN IMMEDIATE")
            existing: sqlite3.Row | None = database.execute(
                "SELECT parent_conversation_id, parent_entry_id FROM conversations WHERE id=?",
                (conversation_id,),
            ).fetchone()
            if existing is not None:
                if (
                    _optional_text(existing, "parent_conversation_id") != parent_conversation_id
                    or _optional_text(existing, "parent_entry_id") != parent_entry_id
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
        if mutation.delete_conversation is not None:
            with self.history_lifetime.exclusive():
                return self._commit(mutation)
        return self._commit(mutation)

    def _commit(self, mutation: DurableMutation) -> CommitResult:
        with closing(self._connect()) as database:
            database.execute("BEGIN IMMEDIATE")
            try:
                existing_submission = _find_submission(database, mutation.submission)
                if existing_submission is not None:
                    return existing_submission
                creation = mutation.create_conversation
                if creation is not None:
                    if _exists(database, "SELECT 1 FROM conversations WHERE id=?", (mutation.conversation_id,)):
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
                    child: sqlite3.Row | None = database.execute(
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
                    # Retain public commit cursors/tombstones, but purge private
                    # checkpoints and effect bodies in the same delete transaction.
                    database.execute(
                        "UPDATE commits SET payload=json_remove(payload, '$._workflow') "
                        "WHERE json_extract(payload, '$.session_id')=?",
                        (mutation.conversation_id,),
                    )
                    task_rows: list[sqlite3.Row] = database.execute(
                        "SELECT id FROM tasks WHERE conversation_id=?", (mutation.conversation_id,),
                    ).fetchall()
                    task_ids = [_text(row, "id") for row in task_rows]
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
                if mutation.submission is not None and _exists(
                    database,
                    "SELECT 1 FROM submissions WHERE id=?",
                    (mutation.submission.submission_id,),
                ):
                    raise IdempotencyConflict("submission ID was already used")
                new_head = mutation.branch_entry_id or (mutation.entries[-1].entry_id if mutation.entries else head)
                new_revision = current_revision + (1 if mutation.branch_entry_id is not None else len(mutation.entries))
                generation_row: sqlite3.Row | None = database.execute(
                    "SELECT path_generation FROM conversations WHERE id=?", (mutation.conversation_id,),
                ).fetchone()
                if generation_row is None:
                    raise KeyError(f"unknown conversation: {mutation.conversation_id}")
                generation = _integer(generation_row, "path_generation") + int(rewrites_path(mutation))
                record = _new_commit(database, "durable", {
                    **mutation_projection(mutation), "_workflow": execution_payload(mutation, new_head),
                    "path_generation": generation, "revision": new_revision,
                })
                sequence = record.sequence
                stored_entries = _write_entries(database, mutation, current_revision, sequence)
                write_display_index(database, mutation, stored_entries)
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
                _write_submission_update(database, mutation.submission_update)
                _write_submission_updates(database, mutation.submission_updates)
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

    def conversation_ids(self) -> tuple[str, ...]:
        with closing(self._connect()) as database:
            rows: Iterator[sqlite3.Row] = iter(database.execute("SELECT id FROM conversations ORDER BY id"))
            return tuple(_text(row, "id") for row in rows)

    def entries(self, conversation_id: str) -> tuple[EntryDraft, ...]:
        with closing(self._connect()) as database:
            return _entries(database, conversation_id)

    @contextmanager
    def history_snapshot(
        self, *, conversation_id: str | None = None, admission: AbstractContextManager[None] | None = None,
    ) -> Generator[HistoryReader, None, None]:
        with self.history_lifetime.read(), closing(self._connect()) as database:
            database.execute("BEGIN")
            try:
                reader = SQLiteHistoryReader(database)
                with admission if admission is not None else nullcontext():
                    if conversation_id is not None:
                        # The first SELECT pins SQLite's deferred snapshot.
                        reader.display_head(conversation_id)
                yield reader
            finally:
                database.execute("ROLLBACK")

    def head(self, conversation_id: str) -> tuple[str | None, int]:
        with closing(self._connect()) as database:
            revision, head = _read_conversation(database, conversation_id)
            return head, revision

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
        _head, revision = self.head(conversation_id)
        self.commit(DurableMutation(conversation_id, revision, delete_conversation=DeleteConversation()))

    def conversation_parent(self, conversation_id: str) -> tuple[str | None, str | None]:
        with closing(self._connect()) as database:
            return _conversation_parent(database, conversation_id)

    def has_conversation(self, conversation_id: str) -> bool:
        with closing(self._connect()) as database:
            row: sqlite3.Row | None = database.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            return bool(row)

    def task(self, task_id: str) -> TaskCheckpoint:
        with closing(self._connect()) as database:
            row: sqlite3.Row | None = database.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown task: {task_id}")
            return task_from_row(row)

    def effect(self, effect_id: str) -> EffectRecord:
        with closing(self._connect()) as database:
            row: sqlite3.Row | None = database.execute("SELECT * FROM effects WHERE id=?", (effect_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown effect: {effect_id}")
            return effect_from_row(row)

    def approval(self, approval_id: str) -> ApprovalRecord:
        with closing(self._connect()) as database:
            rows: list[sqlite3.Row] = database.execute("SELECT state FROM tasks").fetchall()
            for row in rows:
                state: object = json.loads(_text(row, "state"))
                approvals = state.get("approvals") if object_fields(state) else None
                payload = approvals.get(approval_id) if object_fields(approvals) else None
                if object_fields(payload):
                    return ApprovalRecord.from_payload(payload)
            raise KeyError(f"unknown approval: {approval_id}")

    def effects_for_task(self, task_id: str) -> tuple[EffectRecord, ...]:
        with closing(self._connect()) as database:
            rows: list[sqlite3.Row] = database.execute(
                "SELECT id, task_id, category, replay_class, args_digest, status, intent, settlement "
                "FROM effects WHERE task_id=? ORDER BY rowid",
                (task_id,),
            ).fetchall()
            return tuple(effect_from_row(row) for row in rows)

    def artifact(self, handle: str) -> ArtifactRecord:
        with closing(self._connect()) as database:
            row: sqlite3.Row | None = database.execute("SELECT * FROM artifacts WHERE handle=?", (handle,)).fetchone()
            if row is None:
                raise KeyError(f"unknown artifact: {handle}")
            return artifact_from_row(row)

    def artifacts_for_task(self, task_id: str) -> tuple[ArtifactRecord, ...]:
        with closing(self._connect()) as database:
            rows: list[sqlite3.Row] = database.execute("SELECT * FROM artifacts WHERE owner_task_id=? ORDER BY handle", (task_id,)).fetchall()
            return tuple(artifact_from_row(row) for row in rows)

    def artifacts_for_effect(self, effect_id: str) -> tuple[ArtifactRecord, ...]:
        with closing(self._connect()) as database:
            rows: list[sqlite3.Row] = database.execute("SELECT * FROM artifacts WHERE owner_effect_id=? ORDER BY handle", (effect_id,)).fetchall()
            return tuple(artifact_from_row(row) for row in rows)

    def submission(self, submission_id: str) -> SubmissionRecord:
        with closing(self._connect()) as database:
            row: sqlite3.Row | None = database.execute("SELECT * FROM submissions WHERE id=?", (submission_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown submission: {submission_id}")
            return submission_from_row(row)

    def queued_submissions(self, conversation_id: str | None = None) -> tuple[SubmissionRecord, ...]:
        with closing(self._connect()) as database:
            if conversation_id is None:
                rows: list[sqlite3.Row] = database.execute(
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
            row: sqlite3.Row | None = database.execute(
                "SELECT id, fingerprint, revision, commit_seq FROM submissions WHERE conversation_id=? AND request_id=?",
                (conversation_id, request_id),
            ).fetchone()
            if row is None:
                return None
            if _text(row, "fingerprint") != fingerprint:
                raise IdempotencyConflict("request_id was already admitted with different durable result content")
            return CommitResult(_integer(row, "revision"), _integer(row, "commit_seq"), _text(row, "id"))

    def pending_tasks(self) -> tuple[TaskCheckpoint, ...]:
        with closing(self._connect()) as database:
            rows: list[sqlite3.Row] = database.execute(
                "SELECT * FROM tasks WHERE status NOT IN ('completed','failed','cancelled') ORDER BY rowid"
            ).fetchall()
            return tuple(task_from_row(row) for row in rows)

    def finalization_queue(self) -> tuple[str, ...]:
        with closing(self._connect()) as database:
            rows: list[sqlite3.Row] = database.execute(
                "SELECT task_id FROM finalization_queue ORDER BY task_id"
            ).fetchall()
            return tuple(_text(row, "task_id") for row in rows)

    def tasks(self) -> tuple[TaskCheckpoint, ...]:
        with closing(self._connect()) as database:
            rows: list[sqlite3.Row] = database.execute("SELECT * FROM tasks ORDER BY rowid").fetchall()
            return tuple(task_from_row(row) for row in rows)

    def waiting_dependents(self, task_id: str) -> tuple[TaskCheckpoint, ...]:
        with closing(self._connect()) as database:
            rows: list[sqlite3.Row] = database.execute(
                "SELECT tasks.* FROM task_waiters "
                "JOIN tasks ON tasks.id=task_waiters.waiter_task_id "
                "WHERE task_waiters.waited_task_id=? ORDER BY tasks.rowid",
                (task_id,),
            ).fetchall()
            return tuple(task_from_row(row) for row in rows)

    def owned_dependents(self, task_id: str) -> tuple[TaskCheckpoint, ...]:
        with closing(self._connect()) as database:
            rows: list[sqlite3.Row] = database.execute(
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
                row: sqlite3.Row | None = database.execute(
                    "SELECT owner_id, process_id FROM scheduler_owners WHERE id=1"
                ).fetchone()
                now = datetime.now(UTC).isoformat()
                if row is None:
                    database.execute(
                        "INSERT INTO scheduler_owners(id, owner_id, process_id, claimed_at) VALUES(1, ?, ?, ?)",
                        (owner_id, process_id, now),
                    )
                elif _text(row, "owner_id") == owner_id:
                    database.execute(
                        "UPDATE scheduler_owners SET process_id=?, claimed_at=? WHERE id=1",
                        (process_id, now),
                    )
                else:
                    old_pid = _integer(row, "process_id")
                    from codey.durable.process import process_is_alive

                    alive = process_is_alive(old_pid)
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
            row: sqlite3.Row | None = database.execute("SELECT COALESCE(MAX(seq), 0) AS seq FROM commits").fetchone()
            if row is None:
                raise RuntimeError("SQLite did not return a commit sequence")
            return _integer(row, "seq")

    def commits_after(self, sequence: int, *, limit: int = 512) -> tuple[CommitRecord, ...]:
        bounded = max(1, min(2048, int(limit)))
        with closing(self._connect()) as database:
            rows: list[sqlite3.Row] = database.execute(
                "SELECT seq, kind, committed_at, payload FROM commits WHERE seq>? ORDER BY seq LIMIT ?",
                (int(sequence), bounded),
            ).fetchall()
            return tuple(
                CommitRecord(_integer(row, "seq"), _text(row, "kind"), _text(row, "committed_at"),
                             public_commit_payload(_object(row, "payload")))
                for row in rows
            )

    def task_history(self, task_id: str, *, through: int | None = None) -> tuple[TaskTransition, ...]:
        with closing(self._connect()) as database:
            rows: list[sqlite3.Row] = database.execute(
                "SELECT seq, json_extract(payload, '$._workflow') AS workflow FROM commits "
                "WHERE json_extract(payload, '$._workflow') IS NOT NULL "
                "AND (? IS NULL OR seq<=?) ORDER BY seq", (through, through),
            ).fetchall()
            history: list[TaskTransition] = []
            for row in rows:
                raw = _object(row, "workflow")
                task_rows = raw.get("tasks")
                matches = (
                    isinstance(task_rows, list)
                    and any(object_fields(item) and item.get("task_id") == task_id for item in task_rows)
                )
                legacy_task = raw.get("task")
                legacy_match = object_fields(legacy_task) and legacy_task.get("task_id") == task_id
                if matches or legacy_match:
                    history.append(transition_from_payload(_integer(row, "seq"), raw, task_id))
            return tuple(history)

    def task_settlement_history(
        self, task_ids: tuple[str, ...], *, through: int,
    ) -> tuple[TaskSettlementHistory, ...]:
        with closing(self._connect()) as database:
            rows: list[sqlite3.Row] = database.execute(
                "SELECT seq, json_extract(payload, '$._workflow') AS workflow FROM commits "
                "WHERE json_extract(payload, '$._workflow') IS NOT NULL AND seq<=? ORDER BY seq",
                (through,),
            ).fetchall()

            def payloads() -> Iterator[tuple[int, object]]:
                for row in rows:
                    sequence: object = row["seq"]
                    encoded: object = row["workflow"]
                    if type(sequence) is not int or not isinstance(encoded, str):
                        raise ValueError("invalid workflow commit row")
                    value: object = json.loads(encoded)
                    yield sequence, value

            return settlement_histories(task_ids, payloads())


def _entries(database: sqlite3.Connection, conversation_id: str) -> tuple[EntryDraft, ...]:
    rows: list[sqlite3.Row] = database.execute(
        "SELECT id, conversation_id, parent_id, kind, payload, commit_seq, by_task_id "
        "FROM entries WHERE conversation_id=? ORDER BY ordinal", (conversation_id,),
    ).fetchall()
    if not rows and not _exists(database, "SELECT 1 FROM conversations WHERE id=?", (conversation_id,)):
        raise KeyError(f"unknown conversation: {conversation_id}")
    return tuple(entry_from_row(row) for row in rows)


def _conversation_parent(database: sqlite3.Connection, conversation_id: str) -> tuple[str | None, str | None]:
    row: sqlite3.Row | None = database.execute(
        "SELECT parent_conversation_id, parent_entry_id FROM conversations WHERE id=?", (conversation_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown conversation: {conversation_id}")
    return _optional_text(row, "parent_conversation_id"), _optional_text(row, "parent_entry_id")


def _encode(payload: object) -> str:
    return json.dumps(thaw_json(payload), ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def _find_submission(database: sqlite3.Connection, draft: SubmissionDraft | None) -> CommitResult | None:
    if draft is None:
        return None
    row: sqlite3.Row | None = database.execute(
        "SELECT s.id, s.fingerprint, s.revision, s.commit_seq, s.placement, s.mode, s.content, "
        "e.id AS entry_id, e.parent_id, e.kind, e.payload "
        "FROM submissions s LEFT JOIN entries e ON e.id=s.entry_id "
        "WHERE s.conversation_id=? AND s.request_id=?",
        (draft.conversation_id, draft.request_id),
    ).fetchone()
    if row is None:
        return None
    stored_entry = None
    entry_id = _optional_text(row, "entry_id")
    if entry_id is not None:
        stored_entry = EntryDraft(
            entry_id, draft.conversation_id, _optional_text(row, "parent_id"), _text(row, "kind"), _object(row, "payload"),
        )
    stored = SubmissionDraft(
        _text(row, "id"), draft.request_id, draft.conversation_id, stored_entry,
        _text(row, "fingerprint"), placement=_text(row, "placement"), mode=_text(row, "mode"),
        content=_optional_object(row, "content"),
    )
    if not same_submission(stored, draft):
        raise IdempotencyConflict("request_id was already admitted with different input")
    return CommitResult(_integer(row, "revision"), _integer(row, "commit_seq"), _text(row, "id"))


def _new_commit(database: sqlite3.Connection, kind: str, payload: Mapping[str, object]) -> CommitRecord:
    committed_at = datetime.now(UTC).isoformat()
    identity: sqlite3.Row | None = database.execute("SELECT value FROM schema_metadata WHERE key='database_id'").fetchone()
    if identity is None:
        raise SchemaVersionError("durable database identity is missing")
    payload = {**payload, "database_id": _text(identity, "value")}
    cursor = database.execute(
        "INSERT INTO commits(kind, committed_at, payload) VALUES(?, ?, ?)",
        (kind, committed_at, _encode(payload)),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite did not return a commit sequence")
    return CommitRecord(int(cursor.lastrowid), kind, committed_at, public_commit_payload(payload))


def _read_conversation(database: sqlite3.Connection, conversation_id: str) -> tuple[int, str | None]:
    row: sqlite3.Row | None = database.execute("SELECT head_entry_id, revision FROM conversations WHERE id=?", (conversation_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown conversation: {conversation_id}")
    return _integer(row, "revision"), _optional_text(row, "head_entry_id")


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
        and not _exists(
            database,
            "SELECT 1 FROM entries WHERE conversation_id=? AND id=?",
            (mutation.conversation_id, mutation.branch_entry_id),
        )
    ):
        raise CommitConflict("branch entry does not belong to conversation")
    parent = head
    if mutation.entries and mutation.edited_entry_id is None and mutation.create_conversation is None:
        from codey.durable.edit import validate_entry_owners
        owner_ids = {entry.by_task_id for entry in mutation.entries if entry.by_task_id}
        owners: list[TaskCheckpoint] = []
        for task_id in owner_ids:
            row: sqlite3.Row | None = database.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is not None:
                owners.append(task_from_row(row))
        if owners:
            validate_entry_owners(mutation.entries,
                                  context_path(mutation.conversation_id, head,
                                               entries=lambda scope: _entries(database, scope),
                                               parent=lambda scope: _conversation_parent(database, scope)), tuple(owners))
    if mutation.edited_entry_id is not None:
        from codey.durable.edit import validate_edit
        parent = validate_edit(
            mutation,
            context_path(mutation.conversation_id, head,
                         entries=lambda scope: _entries(database, scope),
                         parent=lambda scope: _conversation_parent(database, scope)),
            busy=_exists(
                database,
                "SELECT 1 FROM tasks WHERE conversation_id=? AND status NOT IN ('completed','failed','cancelled') LIMIT 1",
                (mutation.conversation_id,),
            ),
        )
    for entry in mutation.entries:
        if entry.parent_id != parent:
            raise CommitConflict("mutation entries do not extend the active branch")
        if _exists(database, "SELECT 1 FROM entries WHERE id=?", (entry.entry_id,)):
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
    submission: sqlite3.Row | None = database.execute(
        "SELECT conversation_id FROM submissions WHERE id=?",
        (task.submission_id,),
    ).fetchone()
    proposed_matches = (
        proposed_submission is not None
        and proposed_submission.submission_id == task.submission_id
        and proposed_submission.conversation_id == task.conversation_id
    )
    if (submission is None and not proposed_matches) or (
        submission is not None and _text(submission, "conversation_id") != task.conversation_id
    ):
        raise CommitConflict("task must belong to a committed submission")
    row: sqlite3.Row | None = database.execute(
        "SELECT conversation_id, submission_id, revision, provider_snapshot, phase, status, state, schema_version FROM tasks WHERE id=?",
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
    owner: sqlite3.Row | None = database.execute("SELECT id FROM tasks WHERE id=?", (owner_task_id,)).fetchone()
    if owner is None:
        raise CommitConflict("owner task does not exist")
    seen: set[str] = {task.task_id}
    cursor = owner_task_id
    while cursor not in seen:
        seen.add(cursor)
        parent: sqlite3.Row | None = database.execute("SELECT state FROM tasks WHERE id=?", (cursor,)).fetchone()
        if parent is None:
            break
        raw: object = json.loads(_text(parent, "state"))
        next_owner = raw.get("owner_task_id") if object_fields(raw) else None
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
    previous_state = _object(row, "state")
    if (previous_state.get("cancel_requested") and task.phase not in {"cancelled", "failed"}
            and not valid_cancellation_cleanup(previous_state, task)):
        raise CommitConflict("task cancellation barrier is active")
    if not valid_execution_attempt(previous_state, TaskStatus(_text(row, "status")), task, expected_attempt_token):
        raise CommitConflict("task execution attempt changed")
    if task.revision != _integer(row, "revision") + 1:
        raise CommitConflict("task revision changed")
    if (
        _text(row, "conversation_id") != task.conversation_id
        or _text(row, "submission_id") != task.submission_id
        or _text(row, "provider_snapshot") != _encode(task.provider_snapshot)
    ):
        raise CommitConflict("task identity is immutable")
    if not valid_frozen_task_state(previous_state, task.state):
        raise CommitConflict("admitted task state is immutable")
    previous_phase, _version = migrate_checkpoint_phase(_text(row, "phase"), _integer(row, "schema_version"))
    consumed = valid_input_consumption(previous_phase, _text(row, "status"), previous_state, task)
    if not consumed and not valid_task_transition(previous_phase, task.phase):
        raise CommitConflict(f"invalid task phase transition: {previous_phase} -> {task.phase}")
    previous_status = TaskStatus(_text(row, "status"))
    if not consumed and not valid_task_status_transition(previous_status, TaskStatus(str(task.status))):
        raise CommitConflict(f"invalid task status transition: {previous_status} -> {task.status}")


def _validate_effect(database: sqlite3.Connection, mutation: DurableMutation) -> None:
    effects = ((mutation.effect,) if mutation.effect is not None else ()) + mutation.effects + mutation.settlements
    if not effects:
        return
    if mutation.task is None:
        raise CommitConflict("effect mutation requires its task checkpoint")
    for effect in effects:
        task_exists = _exists(database, "SELECT 1 FROM tasks WHERE id=?", (effect.task_id,))
        task_is_new = mutation.task is not None and mutation.task.task_id == effect.task_id
        if not task_exists and not task_is_new:
            raise CommitConflict("effect task does not exist")
        row: sqlite3.Row | None = database.execute(
            "SELECT task_id, category, replay_class, args_digest, status, intent, settlement FROM effects WHERE id=?",
            (effect.effect_id,),
        ).fetchone()
        if row is None:
            if (effect is mutation.effect and mutation.expected_effect_status is not None) or effect in mutation.settlements:
                raise CommitConflict("effect status changed")
            continue
        expected = "pending" if effect in mutation.settlements else mutation.expected_effect_status
        if _column(row, "settlement") is not None:
            raise CommitConflict("effect is already settled")
        if (effect is not mutation.effect and effect not in mutation.settlements) or _text(row, "status") != expected:
            raise CommitConflict("effect status changed")
        original = (_text(row, "task_id"), _text(row, "category"), _text(row, "replay_class"), _text(row, "args_digest"), _text(row, "intent"))
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
        if _exists(database, "SELECT 1 FROM artifacts WHERE handle=?", (artifact.handle,)):
            raise CommitConflict("artifact handle already exists")
        if artifact.owner_task_id is not None:
            task_exists = _exists(database, "SELECT 1 FROM tasks WHERE id=?", (artifact.owner_task_id,))
            if not task_exists and artifact.owner_task_id not in proposed_tasks:
                raise CommitConflict("artifact owner task does not exist")
        if artifact.owner_effect_id is not None:
            effect_exists = _exists(database, "SELECT 1 FROM effects WHERE id=?", (artifact.owner_effect_id,))
            if not effect_exists and artifact.owner_effect_id not in proposed_effects:
                raise CommitConflict("artifact owner effect does not exist")


def _validate_submission_update(
    database: sqlite3.Connection,
    update: SubmissionUpdate | None,
    entries: tuple[EntryDraft, ...],
) -> None:
    if update is None:
        return
    row: sqlite3.Row | None = database.execute(
        "SELECT conversation_id, status, placement, mode, entry_id AS input_entry_id FROM submissions WHERE id=?",
        (update.submission_id,),
    ).fetchone()
    if row is None or _text(row, "conversation_id") != update.conversation_id:
        raise CommitConflict("unknown submission")
    current = SubmissionStatus(_text(row, "status"))
    if current != update.expected_status or (
        current != update.status and not valid_submission_transition(current, update.status)
    ):
        raise CommitConflict("submission status changed")
    if (update.content is not None or update.mode is not None) and (
                current is not SubmissionStatus.QUEUED or _text(row, "placement") != "queued"
                or _column(row, "input_entry_id") is not None or _text(row, "mode") == "write"
                or update.status is not SubmissionStatus.QUEUED
                or update.input_entry_id is not None or update.placement is not None):
        raise CommitConflict("only queued user inputs can be revised")
    if update.answer_entry_id is not None and update.answer_entry_id not in {entry.entry_id for entry in entries}:
        raise CommitConflict("answer entry must be part of the same commit")
    if update.input_entry_id is not None and update.input_entry_id not in {entry.entry_id for entry in entries}:
        raise CommitConflict("placed input entry must be part of the same commit")
    validate_submission_update_state(
        update, current_placement=SubmissionPlacement(_text(row, "placement")),
        current_entry_id=_optional_text(row, "input_entry_id"), current_mode=_text(row, "mode"),
    )


def _write_entries(database: sqlite3.Connection, mutation: DurableMutation, revision: int,
                   sequence: int) -> tuple[EntryDraft, ...]:
    stored = finalized_entries(mutation, sequence)
    for ordinal, entry in enumerate(stored, start=revision + 1):
        database.execute(
            "INSERT INTO entries(id, conversation_id, parent_id, kind, payload, ordinal, commit_seq, by_task_id) VALUES(?,?,?,?,?,?,?,?)",
            (entry.entry_id, entry.conversation_id, entry.parent_id, entry.kind, _encode(entry.payload), ordinal, sequence,
             entry.by_task_id),
        )
    return stored


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
        _write_effect(database, effect, None)


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
) -> None:
    if update is None:
        return
    cursor = database.execute(
        "UPDATE submissions SET status=?, answer_entry_id=?, reason=?, detail=?, entry_id=COALESCE(?, entry_id), placement=COALESCE(?, placement), content=COALESCE(?, content), mode=COALESCE(?, mode) "
        "WHERE id=? AND conversation_id=? AND status=?",
        (
            update.status.value,
            update.answer_entry_id,
            update.reason,
            None if update.detail is None else _encode(update.detail),
            update.input_entry_id,
            None if update.placement is None else SubmissionPlacement(update.placement).value,
            None if update.content is None else _encode(update.content),
            update.mode,
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
) -> None:
    for update in updates:
        _write_submission_update(database, update)
