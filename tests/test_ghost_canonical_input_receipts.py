"""Optional Ghost safety receipts survive faults in the Ghost database."""

from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
from collections.abc import Generator
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from codey.app.context import AppContext
from codey.app.durable_services import DurableAppServices
from codey.durable import DurableSession, MemoryDurableStore, SQLiteDurableStore
from codey.durable.errors import CommitConflict
from codey.durable.input_receipts import InputReceipt, read_input_receipt
from codey.durable.model import CommitResult, DurableMutation, SubmissionPlacement, SubmissionUpdate, TaskCheckpoint
from codey.ghost.continuity import GhostContinuityItem, GhostContinuityStore
from codey.ghost.observations import GhostObservationStore
from codey.ghost.record_store import GhostRecords
from codey.ghost.sqlite import GHOST_SCHEMA_VERSION, GhostSQLite
from codey.ghost.work_queue import GhostWorkQueueStore
from codey.ghost.work_queue_model import GhostWorkItem
from codey.ghost.work_queue_sources import new_item
from tests.manual.ghost_memory_routing.intake_double_fault_probe import Case, cases, run_cell
from tests.test_ghost_received_input_lifetime import _receive, _run


@pytest.mark.parametrize("case", cases())
def test_double_fault_does_not_revive_failed_input(tmp_path: Path, case: Case) -> None:
    result = run_cell(tmp_path, case)
    assert result.valid
    if case.fault == "healthy":
        assert result.observation_rows > 0 and result.continuity_rows > 0
    else:
        assert result.observation_rows == 0 and result.continuity_rows == 0


def _admit(session: DurableSession, task_id: str = "run", *, queued: bool = False) -> None:
    session.admit_input(
        conversation_id="session", request_id=task_id, task_id=task_id, text="input",
        provider_snapshot={}, task_state={"project": "", "input_request": {"task": "input"}},
        placement=SubmissionPlacement.QUEUED if queued else SubmissionPlacement.PLACED,
    )


@contextmanager
def _session(home: Path, accepted: bool | None, *, queued: bool = False) -> Generator[DurableSession]:
    def before(_task: TaskCheckpoint) -> InputReceipt | None:
        return None if accepted is None else InputReceipt("ghost", accepted)

    with closing(DurableSession(SQLiteDurableStore(home / "durable.sqlite3"), before_admit=before)) as session:
        _admit(session, queued=queued)
        yield session


def _append(home: Path, task_id: str = "run") -> bool:
    return GhostObservationStore(home).append_completed(
        run_id=task_id, session_id="session", mode="chat", user_text="input", assistant_text="reply",
        stop_reason="done",
    )


@pytest.mark.parametrize("accepted", [True, False, None])
def test_receipt_is_original_commit_metadata_not_mutable_submission_content(
    tmp_path: Path, accepted: bool | None,
) -> None:
    path = tmp_path / "durable.sqlite3"
    with _session(tmp_path, accepted, queued=True) as session:
        original = session.task("run")
        original_request = original.state.get("input_request")
        session.revise_queued_input("run", expected_revision=original.revision, text="edited input")
        assert session.task("run").state.get("input_request") == original_request
        assert read_input_receipt(path, "run", "ghost") is accepted
        # Even a trusted direct content update cannot rewrite the original
        # admission result that later Ghost consumers read.
        submission = session.submission(original.submission_id)
        current = session.task("run")
        _head, revision = session.head("session")
        session._commit(DurableMutation(
            "session", revision,
            task=replace(current, revision=current.revision + 1),
            submission_update=SubmissionUpdate(
                submission.submission_id, "session", submission.status, submission.status,
                content={"text": "edited input", "input_receipts": {"ghost": accepted is not True}},
            ),
        ))
        assert read_input_receipt(path, "run", "ghost") is accepted
        session.place_submission(original.submission_id)
        session.claim_task("run")
        session.start_task("run", state=dict(session.task("run").state))
        assert read_input_receipt(path, "run", "ghost") is accepted
    with closing(DurableSession(SQLiteDurableStore(path))) as reopened:
        assert reopened.task("run").phase == "running"
        assert read_input_receipt(path, "run", "ghost") is accepted
        assert _append(tmp_path) is (accepted is True)


@pytest.mark.parametrize("accepted", [True, False, None])
def test_caller_cannot_supply_its_own_receipt(tmp_path: Path, accepted: bool | None) -> None:
    observed: list[bool | None] = []
    path = tmp_path / "durable.sqlite3"

    def before(_task: TaskCheckpoint) -> InputReceipt | None:
        return None if accepted is None else InputReceipt("ghost", accepted)

    def publish(_result: CommitResult, mutation: DurableMutation) -> None:
        if mutation.task is not None:
            observed.append(read_input_receipt(path, mutation.task.task_id, "ghost"))

    captured: list[DurableMutation] = []

    def capture(_result: CommitResult, mutation: DurableMutation) -> None:
        captured.append(mutation)

    with closing(DurableSession(MemoryDurableStore(), on_commit=capture)) as source:
        _admit(source)
    incoming = captured[0]
    assert incoming.submission is not None
    forged = replace(incoming, submission=replace(
        incoming.submission,
        content={**dict(incoming.submission.content or {}), "input_receipts": {"ghost": accepted is not True}},
    ))
    with closing(DurableSession(SQLiteDurableStore(path), before_admit=before, on_commit=publish)) as session:
        session._commit(forged)
        assert read_input_receipt(path, "run", "ghost") is accepted
        assert observed == [accepted]
        _admit(session)  # The original request fingerprint remains idempotent.
        assert observed == [accepted]


def test_rejected_canonical_commit_has_no_receipt_and_retry_can_admit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "durable.sqlite3"
    original_commit = SQLiteDurableStore.commit

    def reject(_store: SQLiteDurableStore, _mutation: DurableMutation) -> CommitResult:
        raise CommitConflict("fixture stale revision")

    def before(_task: TaskCheckpoint) -> InputReceipt:
        return InputReceipt("ghost", False)

    with closing(DurableSession(SQLiteDurableStore(path), before_admit=before)) as session:
        monkeypatch.setattr(SQLiteDurableStore, "commit", reject)
        with pytest.raises(CommitConflict):
            _admit(session)
        assert read_input_receipt(path, "run", "ghost") is None
        monkeypatch.setattr(SQLiteDurableStore, "commit", original_commit)
        _admit(session)
        assert read_input_receipt(path, "run", "ghost") is False


@pytest.mark.parametrize("fault", ["absent", "invalid", "missing_task", "missing_receipt", "new_schema"])
def test_bound_authority_failure_does_not_fall_back_to_learning(tmp_path: Path, fault: str) -> None:
    path = tmp_path / "durable.sqlite3"
    with _session(tmp_path, None if fault == "missing_receipt" else True):
        store = GhostObservationStore(tmp_path)
        assert _append(tmp_path) is (fault != "missing_receipt")
    # The Ghost binding survives authority loss and process/store recreation.
    if fault in {"absent", "invalid"}:
        path.unlink()
        if fault == "invalid":
            path.write_bytes(b"not a database")
    elif fault == "new_schema":
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("PRAGMA user_version=999")
    run_id = "missing-run" if fault == "missing_task" else "run"
    assert not _append(tmp_path, run_id)
    assert not store.append_completed(
        run_id=run_id, session_id="session", mode="chat", user_text="input", assistant_text="reply",
        stop_reason="done",
    )
    assert path.exists() is (fault != "absent")


@pytest.mark.parametrize("source", ["task_done", "run_ledger", "work_checkpoint", "review", "continuity"])
def test_fresh_run_source_filter_obeys_failed_receipt(tmp_path: Path, source: str) -> None:
    with _session(tmp_path, False):
        database = GhostSQLite(tmp_path)
        records = GhostRecords(database, "work_queue")
        item: GhostWorkItem | GhostContinuityItem = (
            GhostContinuityItem("item", "recent_focus", "session", "session", "needs review", "task_done",
                                "run", 0.5, 0.5, "2026-11-17T00:00:00Z", "2026-11-17T00:00:00Z")
            if source == "task_done" else _item(source)
        )
        with records.transaction(write=False):
            assert not records.live_sources((item,))


@pytest.mark.parametrize("source", ["hebbian", "research_note"])
def test_independent_knowledge_namespace_remains_available(tmp_path: Path, source: str) -> None:
    with _session(tmp_path, False):
        records = GhostRecords(GhostSQLite(tmp_path), "work_queue")
        item = _item(source)
        continuity = replace(item, id="continuity-item", source="continuity", metadata={"continuity_source": source})
        with records.transaction(write=False):
            assert records.live_sources((item, continuity)) == (item, continuity)
        records.replace_rows((item.to_payload(), continuity.to_payload()))


class _KnowledgeIndex:
    def recent(
        self, limit: int = 20, *, session_id: str = "", types: tuple[str, ...] = (), project: str = "",
    ) -> tuple[dict[str, object], ...]:
        return ({
            "id": "run", "type": "synthesis", "title": "Recovery findings", "body": "Research synthesis.",
            "open_questions": '["Should recovery be reviewed?"]', "updated": "2999-01-01T00:00:00Z",
            "session_id": session_id, "project": project,
        },)


class _Knowledge:
    index = _KnowledgeIndex()


def test_public_refresh_keeps_independent_knowledge_with_failed_run_audit_id(tmp_path: Path) -> None:
    with _session(tmp_path, False):
        continuity = GhostContinuityStore(tmp_path)
        result = continuity.sync_from_sources(knowledge_store=_Knowledge(), session_id="session", run_id="run")
        assert result.ok
        assert any(item.source == "research_note" and item.source_ref == "run" for item in continuity.list_items())
        queue = GhostWorkQueueStore(tmp_path)
        assert queue.sync_from_sources(continuity_store=continuity, session_id="session", run_id="run").ok
        assert queue.list_items(status="queued", session_id="session")


def _item(source: str) -> GhostWorkItem:
    return new_item(
        scope="session", scope_ref="session", kind="open_question", status="candidate", source=source,
        source_ref="run", title="needs review", why_now="new fact", priority=0.5, confidence=0.5,
        evidence_refs=(), run_refs=("run",), now="2026-11-17T00:00:00Z",
    )


def _source_payload(source: str) -> str:
    payload: dict[str, object] = {"source": source, "source_ref": "run"}
    return json.dumps(payload)


@pytest.mark.parametrize("collection", ["continuity", "work_queue"])
@pytest.mark.parametrize("operation", ["insert", "update"])
def test_direct_run_source_sql_cannot_bypass_failed_receipt(
    tmp_path: Path, collection: str, operation: str,
) -> None:
    with _session(tmp_path, False):
        database = GhostSQLite(tmp_path)
        with database.connection() as connection:
            if operation == "update":
                connection.execute(
                    "INSERT INTO memory_records(collection,id,scope,scope_ref,status,payload) VALUES(?,?,?,?,?,?)",
                    (collection, "item", "session", "session", "open",
                     _source_payload("research_note")),
                )
        with (pytest.raises(sqlite3.IntegrityError, match="unaccepted Ghost run source"),
              database.connection() as connection):
            payload = _source_payload("run_ledger")
            if operation == "insert":
                connection.execute(
                    "INSERT INTO memory_records(collection,id,scope,scope_ref,status,payload) VALUES(?,?,?,?,?,?)",
                    (collection, "item", "session", "session", "open", payload),
                )
            else:
                connection.execute("UPDATE memory_records SET payload=? WHERE collection=? AND id='item'",
                                   (payload, collection))


@pytest.mark.parametrize("operation", ["insert", "update"])
def test_direct_observation_sql_cannot_bypass_failed_receipt(tmp_path: Path, operation: str) -> None:
    assert _append(tmp_path, "standalone-seed")
    with (_session(tmp_path, False), pytest.raises(sqlite3.IntegrityError, match="unaccepted Ghost input"),
          GhostSQLite(tmp_path).connection() as connection):
        if operation == "update":
            connection.execute("UPDATE observation_events SET run_id='run' WHERE run_id='standalone-seed'")
        else:
            connection.execute(
                "INSERT INTO observation_events(run_id,session_id,project,mode,user_text,assistant_text,"
                "stop_reason,committed,provider_id,ts) "
                "SELECT 'run',session_id,project,mode,user_text,assistant_text,stop_reason,committed,provider_id,ts "
                "FROM observation_events WHERE run_id='standalone-seed'",
            )


@pytest.mark.parametrize("value", [1, "true", None, {"accepted": True}])
def test_malformed_original_receipt_cannot_grant_learning(tmp_path: Path, value: object) -> None:
    with _session(tmp_path, True):
        path = tmp_path / "durable.sqlite3"
        # Simulate corrupt canonical metadata, not request-level authority.
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(
                "UPDATE commits SET payload=json_set(payload,'$.projections[' || "
                "(SELECT key FROM json_each(commits.payload,'$.projections') "
                "WHERE json_extract(value,'$.type')='submission_admitted') || '].input_receipts.ghost',json(?)) "
                "WHERE seq=(SELECT commit_seq FROM submissions WHERE request_id='run')",
                (json.dumps(value),),
            )
        with pytest.raises(ValueError, match="invalid input receipt"):
            read_input_receipt(path, "run", "ghost")
        assert not _append(tmp_path)


def test_old_connection_cannot_write_after_authority_schema_upgrade(tmp_path: Path) -> None:
    with _session(tmp_path, True):
        database = GhostSQLite(tmp_path)
        with database.connection() as connection:
            for name in ("ghost_reject_unaccepted_observation_insert", "ghost_reject_unaccepted_observation_update",
                         "ghost_reject_unaccepted_run_source_insert", "ghost_reject_unaccepted_run_source_update"):
                connection.execute(f"DROP TRIGGER {name}")
            connection.execute("DROP TABLE ghost_input_authority")
            connection.execute("PRAGMA user_version=6")
        # Keep a schema-6-era connection open while a new build upgrades.
        # It has no receipt function and cannot silently skip the new check.
        with (closing(sqlite3.connect(database.path)) as connection, connection,
              pytest.raises(sqlite3.OperationalError, match="codey_input_receipt_ok")):
            database = GhostSQLite(tmp_path)
            connection.execute(
                "INSERT INTO memory_records(collection,id,scope,scope_ref,status,payload) VALUES(?,?,?,?,?,?)",
                ("continuity", "item", "session", "session", "open",
                 _source_payload("task_done")),
            )
        with database.connection() as connection:
            version: sqlite3.Row | None = connection.execute("PRAGMA user_version").fetchone()
            number: object = None
            if version is not None:
                number = version[0]
            assert number == GHOST_SCHEMA_VERSION == 7


def test_custom_canonical_database_name_uses_the_actual_app_binding(tmp_path: Path) -> None:
    services = DurableAppServices.open(tmp_path / "tasks.sqlite3", publish=None, dispatch=lambda _task: True, start=False)
    try:
        _admit(services.session)
        assert read_input_receipt(tmp_path / "tasks.sqlite3", "run", "ghost") is True
        assert _append(tmp_path)
    finally:
        assert services.close()


def _crash_after_failed_intake(home: str) -> None:
    root = Path(home)
    database = GhostSQLite(root)
    with database.connection() as connection:
        connection.execute(
            "CREATE TRIGGER reject_origin BEFORE INSERT ON ghost_input_origins "
            "BEGIN SELECT RAISE(ABORT,'fixture origin failure'); END",
        )
        connection.execute(
            "CREATE TRIGGER reject_quarantine BEFORE INSERT ON ghost_retired_inputs "
            "BEGIN SELECT RAISE(ABORT,'fixture quarantine failure'); END",
        )
    state = AppContext(root, start_scheduler=False)
    _receive(state, "")
    assert read_input_receipt(root / "durable.sqlite3", "received-run", "ghost") is False
    os._exit(23)


def test_failed_input_receipt_survives_process_crash_and_new_input_still_learns(tmp_path: Path) -> None:
    process = multiprocessing.get_context("spawn").Process(target=_crash_after_failed_intake, args=(str(tmp_path),))
    process.start()
    try:
        process.join(30)
        assert process.exitcode == 23
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)
        process.close()
    with GhostSQLite(tmp_path).connection() as connection:
        connection.execute("DROP TRIGGER reject_origin")
        connection.execute("DROP TRIGGER reject_quarantine")
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        request = _receive(state, "")  # Idempotent original input, not a new receipt.
        _run(state, request)
        observations = GhostObservationStore(tmp_path)
        assert not observations.read_all()
        _run(state, _receive(state, "", run_id="new-run"))
        assert [row.get("run_id") for row in observations.read_all()] == ["new-run"]


def test_unavailable_ghost_database_does_not_erase_failed_input_receipt(tmp_path: Path) -> None:
    with closing(AppContext(tmp_path, start_scheduler=False)) as state:
        path = tmp_path / "ghost.sqlite3"
        assert not path.exists()
        path.mkdir()
        try:
            request = _receive(state, "")
            assert read_input_receipt(tmp_path / "durable.sqlite3", request.run_id, "ghost") is False
        finally:
            path.rmdir()
        _run(state, request)
        assert not GhostObservationStore(tmp_path).read_all()
