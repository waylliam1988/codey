from __future__ import annotations

import subprocess
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codey.types.json_value import JsonObject, JsonValue
from codey.utils.object_fields import object_fields
from codey.workspace.changes import ChangeTracker, SnapshotStore
from codey.workspace.task_changes import collect_task_changes

if TYPE_CHECKING:
    from codey.app.context import AppContext


def _task_tracker(state: AppContext, root: Path, task_id: str, *, source_id: str = "") -> ChangeTracker:
    from codey.operations.task_changes import task_change_tracker

    value: object = task_change_tracker(state, root, task_id, source_id=source_id)
    assert isinstance(value, ChangeTracker)
    return value


def _files(changes: dict[str, object]) -> list[dict[str, object]]:
    value = changes["files"]
    assert isinstance(value, list)
    items: list[object] = value
    rows: list[dict[str, object]] = []
    for item in items:
        assert object_fields(item)
        rows.append(item)
    return rows


def _diff(changes: dict[str, object]) -> str:
    value = changes["diff"]
    assert isinstance(value, str)
    return value


@pytest.mark.parametrize("git", [False, True])
def test_only_task_writes_enter_projection_even_in_dirty_workspace(tmp_path: Path, git: bool) -> None:
    project, state = tmp_path / "project", tmp_path / "state"
    project.mkdir()
    if git:
        subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / "old.py").write_text("user content\n")
    (project / "untouched.py").write_text("preexisting untracked\n")
    tracker = ChangeTracker(project, SnapshotStore(state, task_id="first"))
    assert collect_task_changes(project, tracker)["changed_count"] == 0
    tracker.capture_before("old.py")
    (project / "old.py").write_text("task content\n")
    tracker.capture_after("old.py")
    changes = collect_task_changes(project, tracker)
    assert [f["path"] for f in _files(changes)] == ["old.py"]
    assert "-user content" in _diff(changes) and "+task content" in _diff(changes)
    reopened = ChangeTracker(project, SnapshotStore(state, task_id="first"))
    assert collect_task_changes(project, reopened) == changes
    second = ChangeTracker(project, SnapshotStore(state, task_id="second"))
    assert not collect_task_changes(project, second)["files"]
    second.capture_before("old.py")
    (project / "old.py").write_text("second task\n")
    second.capture_after("old.py")
    assert "-task content" in _diff(collect_task_changes(project, second))
    assert not collect_task_changes(project, reopened)["ok"]


def test_failed_edit_baseline_does_not_claim_external_change(tmp_path: Path) -> None:
    (tmp_path / "a").write_text("before")
    tracker = ChangeTracker(tmp_path)
    tracker.capture_before("a")
    (tmp_path / "a").write_text("external")
    assert collect_task_changes(tmp_path, tracker)["files"] == []


def test_task_scopes_share_the_original_workspace_writer_lock(tmp_path: Path) -> None:
    stores = [SnapshotStore(tmp_path / "state", task_id=scope) for scope in ("", "first", "second")]
    assert len({store.path_for(tmp_path) for store in stores}) == 3
    assert len({store.writer_lock_path(tmp_path) for store in stores}) == 1


def test_task_create_and_revert_are_net_changes(tmp_path: Path) -> None:
    tracker = ChangeTracker(tmp_path)
    tracker.capture_before("new")
    (tmp_path / "new").write_text("created")
    tracker.capture_after("new")
    assert _files(collect_task_changes(tmp_path, tracker))[0]["status"] == "A"
    (tmp_path / "new").unlink()
    tracker.capture_after("new")
    assert collect_task_changes(tmp_path, tracker)["changed_count"] == 0


def test_original_checkpoint_owns_scope_and_resume_reuses_it(tmp_path: Path) -> None:
    from codey.app.context import AppContext
    from codey.operations.task_changes import RunChangeTracker

    root, home = tmp_path / "project", tmp_path / "state"
    root.mkdir()
    (root / "a").write_text("before")
    with closing(AppContext(home, start_scheduler=False)) as state:
        for task in ("first", "second", "resume"):
            state.durable.admit_input(conversation_id="s", request_id=task, task_id=task,
                                      text="edit", task_state={"project": str(root)}, provider_snapshot={})
        first = _task_tracker(state, root, "first")
        first.capture_before("a")
        (root / "a").write_text("first edit")
        first.capture_after("a")
        second = _task_tracker(state, root, "second")
        assert collect_task_changes(root, second)["changed_count"] == 0
        resumed = _task_tracker(state, root, "resume", source_id="first")
        assert state.durable.task("resume").state["change_scope"] == "first"
        assert isinstance(resumed, RunChangeTracker)
        assert collect_task_changes(root, resumed)["changed_count"] == 0
        assert collect_task_changes(root, resumed.recovery)["changed_count"] == 1
    with closing(AppContext(home, start_scheduler=False)) as state:
        resumed = _task_tracker(state, root, "resume")
        assert isinstance(resumed, RunChangeTracker)
        assert collect_task_changes(root, resumed)["changed_count"] == 0
        assert collect_task_changes(root, resumed.recovery)["changed_count"] == 1


def test_corrupt_scoped_baseline_never_becomes_an_empty_change_set(tmp_path: Path) -> None:
    from codey.storage.local_store import StoreCorruption

    project, home = tmp_path / "project", tmp_path / "state"
    project.mkdir()
    (project / "a").write_text("before")
    store = SnapshotStore(home, task_id="r")
    tracker = ChangeTracker(project, store)
    tracker.capture_before("a")
    (project / "a").write_text("after")
    tracker.capture_after("a")
    next((store.dir_for(project) / "baselines").glob("*.txt")).unlink()
    with pytest.raises(StoreCorruption, match="body unavailable"):
        ChangeTracker(project, store)


def test_recovery_ui_does_not_invent_an_unbound_task_scope(tmp_path: Path) -> None:
    from codey.app.context import AppContext

    root, home = tmp_path / "project", tmp_path / "state"
    root.mkdir()
    (root / "a").write_text("before")
    with closing(AppContext(home, start_scheduler=False)) as state:
        state.durable.admit_input(conversation_id="s", request_id="r", task_id="r", text="read",
                                  task_state={"project": str(root)}, provider_snapshot={})
        orphan = ChangeTracker(root, SnapshotStore(home, task_id="r"))
        orphan.capture_before("a")
        (root / "a").write_text("unowned")
        orphan.capture_after("a")
        selected = state.change_tracker_for(root, persistent=True)
        assert not selected.has_snapshots
        assert "change_scope" not in state.durable.task("r").state


def test_tracker_cache_keeps_recent_scopes_and_retires_evicted_writers(tmp_path: Path) -> None:
    from codey.app.change_tracking import change_tracker_for
    from codey.app.context import AppContext

    root = tmp_path / "project"
    root.mkdir()
    with closing(AppContext(tmp_path / "state", start_scheduler=False, memory_enabled=False)) as state:
        ephemeral = change_tracker_for(state, root, persistent=False, task_id="ephemeral", limit=2)
        assert ephemeral.store is None and not state.change_trackers
        first = change_tracker_for(state, root, persistent=True, task_id="first", limit=2)
        second = change_tracker_for(state, root, persistent=True, task_id="second", limit=2)
        assert change_tracker_for(state, root, persistent=True, task_id="first", limit=2) is first
        third = change_tracker_for(state, root, persistent=True, task_id="third", limit=2)
        assert list(state.change_trackers.values()) == [first, third]
        assert second.store is None
        assert first.store is not None and third.store is not None
        revived = change_tracker_for(state, root, persistent=True, task_id="second", limit=2)
        assert revived is not second and revived.store is not None
        assert first.store is None


def test_recovery_skips_owned_scopes_without_a_baseline(tmp_path: Path) -> None:
    from codey.app.context import AppContext
    from codey.operations.task_changes import RunChangeTracker

    root = tmp_path / "project"
    root.mkdir()
    (root / "a").write_text("before", encoding="utf-8")
    with closing(AppContext(tmp_path / "state", start_scheduler=False, memory_enabled=False)) as state:
        for task in ("older", "newer"):
            state.durable.admit_input(
                conversation_id="s", request_id=task, task_id=task, text="edit",
                task_state={"project": str(root)}, provider_snapshot={},
            )
        older = _task_tracker(state, root, "older")
        older.capture_before("a")
        (root / "a").write_text("after", encoding="utf-8")
        older.capture_after("a")
        newer = _task_tracker(state, root, "newer")
        assert newer.store is not None and not newer.store.path_for(root).exists()

        selected = state.change_tracker_for(root, persistent=True)

        assert isinstance(older, RunChangeTracker)
        assert selected is older.recovery
        assert selected.store is not None and selected.store.task_id == "older"
        assert collect_task_changes(root, selected)["changed_count"] == 1


@pytest.mark.parametrize("project", [42, ["invalid"]])
def test_recovery_rejects_non_path_project_metadata(tmp_path: Path, project: JsonValue) -> None:
    from codey.app.context import AppContext

    root = tmp_path / "project"
    root.mkdir()
    with closing(AppContext(tmp_path / "state", start_scheduler=False, memory_enabled=False)) as state:
        fields: JsonObject = {"project": project, "change_scope": "scope"}
        state.durable.admit_input(
            conversation_id="s", request_id="r", task_id="r", text="edit", task_state=fields, provider_snapshot={},
        )
        with pytest.raises(TypeError):
            state.change_tracker_for(root, persistent=True)


@pytest.mark.parametrize("scope", ["", 42])
def test_recovery_rejects_invalid_admitted_change_scope(tmp_path: Path, scope: JsonValue) -> None:
    from codey.app.context import AppContext

    root = tmp_path / "project"
    root.mkdir()
    with closing(AppContext(tmp_path / "state", start_scheduler=False, memory_enabled=False)) as state:
        fields: JsonObject = {"project": str(root), "change_scope": scope}
        state.durable.admit_input(
            conversation_id="s", request_id="r", task_id="r", text="edit", task_state=fields, provider_snapshot={},
        )
        with pytest.raises(ValueError, match="invalid task change baseline scope"):
            state.change_tracker_for(root, persistent=True)
