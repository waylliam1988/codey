"""Schema-six projections cannot grant themselves a canonical input receipt."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from codey.app.api import ghost_control_surface
from codey.app.context import AppContext
from codey.durable import DurableSession, SQLiteDurableStore
from codey.durable.input_receipts import InputReceipt, read_input_receipt
from codey.durable.model import TaskCheckpoint
from codey.ghost.continuity import GhostContinuityItem, _item_event, build_ghost_continuity
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.schema import GhostSignal
from codey.ghost.sqlite import GHOST_SCHEMA_VERSION, GhostSQLite
from codey.ghost.work_queue_events import _observed_event
from codey.ghost.work_queue_sources import new_item
from codey.storage.local_store import session_key
from tests.test_ghost_canonical_input_receipts import _Knowledge
from tests.test_ghost_received_input_lifetime import _receive, _run

NOW = "2026-11-17T00:00:00Z"
MARKER = "Migration marker is cobalt harbor."


def _legacy_projection(home: Path) -> GhostControlSurface:
    """Create valid pre-authority data, then expose its actual old schema."""
    surface = GhostControlSurface.from_state_home(home)
    assert surface.observations is not None and surface.continuity is not None and surface.work_queue is not None
    project = str(home / "project")
    assert surface.observations.append_completed(
        run_id="legacy-run", session_id="session", project=project, mode="chat",
        user_text=MARKER, assistant_text="The marker is cobalt harbor.", stop_reason="done",
    )
    continuity = GhostContinuityItem(
        id="legacy-context", kind="recent_focus", scope="session", scope_ref="session",
        text=MARKER, source="run_ledger", source_ref="legacy-run", weight=1.0, confidence=1.0,
        created_at=NOW, updated_at=NOW,
    )
    with surface.continuity.records.transaction():
        surface.continuity.records.append((_item_event(continuity, action="upsert"),))
        surface.continuity.records.replace_rows((continuity.to_payload(),))
    work = new_item(
        kind="open_question", status="queued", scope="session", scope_ref=session_key("session"), title=MARKER,
        why_now="Review the old marker.", priority=1.0, confidence=1.0, source="work_checkpoint",
        source_ref="legacy-run", evidence_refs=(), run_refs=("legacy-run",), now=NOW,
    )
    with surface.work_queue.records.transaction():
        surface.work_queue.records.append((_observed_event(work, ts=NOW),))
        surface.work_queue.records.replace_rows((work.to_payload(),))
    assert surface.observations.read_committed(session_id="session")
    assert surface.continuity.list_items(session_id="session")
    assert surface.work_queue.list_items(status="queued", session_id="session")
    with surface.observations.database.connection() as connection:
        for name in (
            "ghost_reject_unaccepted_observation_insert", "ghost_reject_unaccepted_observation_update",
            "ghost_reject_unaccepted_run_source_insert", "ghost_reject_unaccepted_run_source_update",
        ):
            connection.execute(f"DROP TRIGGER {name}")
        connection.execute("DROP TABLE ghost_input_authority")
        connection.execute("PRAGMA user_version=6")
    return surface


def _canonical(home: Path, accepted: bool | None) -> None:
    def receipt(_task: TaskCheckpoint) -> InputReceipt | None:
        return None if accepted is None else InputReceipt("ghost", accepted)

    with closing(DurableSession(SQLiteDurableStore(home / "durable.sqlite3"), before_admit=receipt)) as session:
        session.admit_input(
            conversation_id="session", request_id="legacy-request", task_id="legacy-run", text=MARKER,
            provider_snapshot={}, task_state={"project": str(home / "project"), "input_request": {"task": MARKER}},
        )
    assert read_input_receipt(home / "durable.sqlite3", "legacy-run", "ghost") is accepted


def _visible(surface: GhostControlSurface, consumer: str, project: str) -> bool:
    if consumer == "observation":
        assert surface.observations is not None
        return bool(surface.observations.retrieve("cobalt harbor", session_id="session", project=project))
    if consumer == "continuity":
        assert surface.continuity is not None
        return MARKER.rstrip(".") in build_ghost_continuity(surface.continuity, session_id="session", project=project).text
    assert surface.work_queue is not None
    if consumer == "work_queue":
        return bool(surface.work_queue.list_items(status="queued", session_id="session", project=project))
    if consumer == "claim":
        result = surface.work_queue.claim_next(
            session_id="session", project=project, run_id="consumer-run", user_request="continue",
        )
        return result.item is not None
    raise ValueError(consumer)


@pytest.mark.parametrize("accepted", [None, False, True])
@pytest.mark.parametrize("consumer", ["observation", "continuity", "work_queue", "claim"])
@pytest.mark.parametrize("reopen", [False, True])
def test_upgrade_requires_original_receipt_before_consuming_existing_run_projection(
    tmp_path: Path, accepted: bool | None, consumer: str, reopen: bool,
) -> None:
    original = _legacy_projection(tmp_path)
    _canonical(tmp_path, accepted)
    upgraded = GhostSQLite(tmp_path)
    with upgraded.connection() as connection:
        row: sqlite3.Row | None = connection.execute("PRAGMA user_version").fetchone()
        assert row is not None
        version: object = row[0]
        assert version == GHOST_SCHEMA_VERSION
    surface = GhostControlSurface.from_state_home(tmp_path) if reopen else original
    visible = _visible(surface, consumer, str(tmp_path / "project"))
    assert read_input_receipt(tmp_path / "durable.sqlite3", "legacy-run", "ghost") is accepted
    assert visible is (accepted is True)


@pytest.mark.parametrize("accepted", [None, False])
def test_reading_and_refusing_claim_preserve_original_rows_events_and_receipt(tmp_path: Path, accepted: bool | None) -> None:
    surface = _legacy_projection(tmp_path)
    _canonical(tmp_path, accepted)
    GhostSQLite(tmp_path)
    assert surface.observations is not None and surface.continuity is not None and surface.work_queue is not None
    observations = surface.observations.read_all()
    context_events = surface.continuity.records.events()
    work_events = surface.work_queue.records.events()
    for consumer in ("observation", "continuity", "work_queue", "claim"):
        assert not _visible(surface, consumer, str(tmp_path / "project"))
    assert surface.observations.read_all() == observations
    assert surface.continuity.records.events() == context_events
    assert surface.work_queue.records.events() == work_events
    assert len(surface.continuity.records.rows()) == len(surface.work_queue.records.rows()) == 1
    assert read_input_receipt(tmp_path / "durable.sqlite3", "legacy-run", "ghost") is accepted


def test_observation_receipt_filter_precedes_recent_limit(tmp_path: Path) -> None:
    surface = _legacy_projection(tmp_path)
    assert surface.observations is not None
    assert surface.observations.append_completed(
        run_id="unaccepted-newer", session_id="session", mode="chat", user_text=MARKER,
        assistant_text="Unknown old source.", stop_reason="done",
    )
    _canonical(tmp_path, True)
    GhostSQLite(tmp_path)
    rows = surface.observations.read_committed(session_id="session", limit=1)
    assert [row["run_id"] for row in rows] == ["legacy-run"]
    assert len(surface.observations.read_all()) == 2


@pytest.mark.parametrize("consumer", ["observation", "continuity", "work_queue", "claim"])
@pytest.mark.parametrize("fault", ["missing", "schema", "retired"])
def test_existing_projection_fails_closed_after_authority_loss_or_retirement(
    tmp_path: Path, consumer: str, fault: str,
) -> None:
    surface = _legacy_projection(tmp_path)
    _canonical(tmp_path, True)
    GhostSQLite(tmp_path)
    authority = tmp_path / "durable.sqlite3"
    if fault == "missing":
        authority.rename(tmp_path / "unavailable.sqlite3")
    elif fault == "schema":
        with closing(sqlite3.connect(authority)) as database, database:
            database.execute("PRAGMA user_version=999")
    else:
        assert surface.observations is not None
        with surface.observations.database.connection() as database:
            database.execute("INSERT INTO ghost_retired_inputs(kind,identity) VALUES('observation','legacy-run')")
    assert not _visible(surface, consumer, str(tmp_path / "project"))
    if fault == "missing":
        assert not authority.exists()


@pytest.mark.parametrize("accepted", [None, False])
@pytest.mark.parametrize("source", ["research_note", "hebbian"])
def test_independent_knowledge_and_manual_preferences_still_have_consumers(
    tmp_path: Path, accepted: bool | None, source: str,
) -> None:
    surface = _legacy_projection(tmp_path)
    _canonical(tmp_path, accepted)
    GhostSQLite(tmp_path)
    assert surface.continuity is not None and surface.work_queue is not None
    if source == "research_note":
        assert surface.continuity.sync_from_sources(
            knowledge_store=_Knowledge(), session_id="session", run_id="legacy-run",
        ).ok
        assert surface.work_queue.sync_from_sources(
            continuity_store=surface.continuity, session_id="session", run_id="legacy-run",
        ).ok
        assert surface.work_queue.list_items(status="queued", session_id="session")
    else:
        assert surface.inbox is not None and surface.hebbian is not None
        quote = "Use concise answers."
        signal = GhostSignal(
            kind="style_preference", scope="user", summary=quote, evidence_quote=quote,
            confidence=1.0, source="manual", metadata={"conflict_key": "reply_length", "value_key": "concise"},
        )
        candidates = surface.inbox.ingest_signals((signal,), session_id="session", run_id="legacy-run", user_text=quote)
        assert len(candidates) == 1
        candidate = surface.inbox.review_candidate(candidates[0].id, "accept", reviewed_by="operator")
        assert candidate is not None and surface.hebbian.reinforce_candidate(candidate).applied
        assert surface.continuity.sync_from_sources(
            hebbian_store=surface.hebbian, session_id="session", run_id="legacy-run",
        ).ok
    assert any(item.source == source for item in surface.continuity.list_items(session_id="session"))
    assert read_input_receipt(tmp_path / "durable.sqlite3", "legacy-run", "ghost") is accepted


@pytest.mark.parametrize("accepted", [None, False])
def test_rebuild_sleep_compaction_and_reopen_do_not_reauthorize_old_input(tmp_path: Path, accepted: bool | None) -> None:
    surface = _legacy_projection(tmp_path)
    _canonical(tmp_path, accepted)
    surface = GhostControlSurface.from_state_home(tmp_path)
    assert surface.continuity is not None and surface.work_queue is not None and surface.sleep is not None
    assert surface.continuity.rebuild_from_events()
    assert surface.work_queue.rebuild_from_events()
    report = surface.sleep.run_once(
        inbox_store=surface.inbox, hebbian_store=surface.hebbian, continuity_store=surface.continuity,
        work_queue_store=surface.work_queue, affinity_store=surface.affinity, session_id="session", trigger="manual",
    )
    assert all(step.ok for step in report.steps), report
    assert surface.continuity.compact_if_needed()["ok"] is True
    assert surface.work_queue.compact_if_needed()["ok"] is True
    reopened = GhostControlSurface.from_state_home(tmp_path)
    for consumer in ("observation", "continuity", "work_queue", "claim"):
        assert not _visible(reopened, consumer, str(tmp_path / "project"))
    assert read_input_receipt(tmp_path / "durable.sqlite3", "legacy-run", "ghost") is accepted


@pytest.mark.parametrize("accepted", [None, False])
def test_actual_application_keeps_new_input_learning_after_legacy_isolation(tmp_path: Path, accepted: bool | None) -> None:
    suffix = hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12]
    home = tmp_path.parent / ("legacy-app-" + suffix)
    _legacy_projection(home)
    _canonical(home, accepted)
    with closing(AppContext(home, start_scheduler=False)) as state:
        surface = ghost_control_surface(state)
        assert surface.observations is not None
        assert surface.observations.read_committed(scope="user") == ()
        _run(state, _receive(state, str(home / "project"), run_id="healthy-new-input"))
        rows = surface.observations.read_committed(scope="user")
        assert [row["run_id"] for row in rows] == ["healthy-new-input"]
        assert len(surface.observations.read_all()) == 2
    reopened = GhostControlSurface.from_state_home(home)
    assert reopened.observations is not None
    assert [row["run_id"] for row in reopened.observations.read_committed(scope="user")] == ["healthy-new-input"]
    assert read_input_receipt(home / "durable.sqlite3", "legacy-run", "ghost") is accepted
