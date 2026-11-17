"""Schema-six projections cannot grant themselves a canonical input receipt."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from codey.durable import DurableSession, SQLiteDurableStore
from codey.durable.input_receipts import InputReceipt, read_input_receipt
from codey.durable.model import TaskCheckpoint
from codey.ghost.continuity import GhostContinuityItem, _item_event, build_ghost_continuity
from codey.ghost.control_surface import GhostControlSurface
from codey.ghost.sqlite import GHOST_SCHEMA_VERSION, GhostSQLite
from codey.ghost.work_queue_events import _observed_event
from codey.ghost.work_queue_sources import new_item
from codey.storage.local_store import session_key

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
