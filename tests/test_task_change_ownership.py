from __future__ import annotations

import subprocess
from contextlib import closing

import pytest

from codey.workspace.changes import ChangeTracker, SnapshotStore
from codey.workspace.task_changes import collect_task_changes


@pytest.mark.parametrize("git", [False, True])
def test_only_task_writes_enter_projection_even_in_dirty_workspace(tmp_path, git):
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
    assert [f["path"] for f in changes["files"]] == ["old.py"]
    assert "-user content" in changes["diff"] and "+task content" in changes["diff"]
    reopened = ChangeTracker(project, SnapshotStore(state, task_id="first"))
    assert collect_task_changes(project, reopened) == changes
    second = ChangeTracker(project, SnapshotStore(state, task_id="second"))
    assert not collect_task_changes(project, second)["files"]
    second.capture_before("old.py")
    (project / "old.py").write_text("second task\n")
    second.capture_after("old.py")
    assert "-task content" in collect_task_changes(project, second)["diff"]
    assert not collect_task_changes(project, reopened)["ok"]


def test_failed_edit_baseline_does_not_claim_external_change(tmp_path):
    (tmp_path / "a").write_text("before")
    tracker = ChangeTracker(tmp_path)
    tracker.capture_before("a")
    (tmp_path / "a").write_text("external")
    assert collect_task_changes(tmp_path, tracker)["files"] == []


def test_task_scopes_share_the_original_workspace_writer_lock(tmp_path):
    stores = [SnapshotStore(tmp_path / "state", task_id=scope) for scope in ("", "first", "second")]
    assert len({store.path_for(tmp_path) for store in stores}) == 3
    assert len({store.writer_lock_path(tmp_path) for store in stores}) == 1


def test_task_create_and_revert_are_net_changes(tmp_path):
    tracker = ChangeTracker(tmp_path)
    tracker.capture_before("new")
    (tmp_path / "new").write_text("created")
    tracker.capture_after("new")
    assert collect_task_changes(tmp_path, tracker)["files"][0]["status"] == "A"
    (tmp_path / "new").unlink()
    tracker.capture_after("new")
    assert collect_task_changes(tmp_path, tracker)["changed_count"] == 0


def test_original_checkpoint_owns_scope_and_resume_reuses_it(tmp_path):
    from codey.app.context import AppContext
    from codey.operations.task_changes import task_change_tracker

    root, home = tmp_path / "project", tmp_path / "state"
    root.mkdir()
    (root / "a").write_text("before")
    with closing(AppContext(home, start_scheduler=False)) as state:
        for task in ("first", "second", "resume"):
            state.durable.admit_input(conversation_id="s", request_id=task, task_id=task,
                                      text="edit", task_state={"project": str(root)}, provider_snapshot={})
        first = task_change_tracker(state, root, "first")
        first.capture_before("a")
        (root / "a").write_text("first edit")
        first.capture_after("a")
        second = task_change_tracker(state, root, "second")
        assert collect_task_changes(root, second)["changed_count"] == 0
        resumed = task_change_tracker(state, root, "resume", source_id="first")
        assert state.durable.task("resume").state["change_scope"] == "first"
        assert collect_task_changes(root, resumed)["changed_count"] == 1
    with closing(AppContext(home, start_scheduler=False)) as state:
        resumed = task_change_tracker(state, root, "resume")
        assert collect_task_changes(root, resumed)["changed_count"] == 1


def test_corrupt_scoped_baseline_never_becomes_an_empty_change_set(tmp_path):
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


def test_recovery_ui_does_not_invent_an_unbound_task_scope(tmp_path):
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
