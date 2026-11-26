from __future__ import annotations

from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

from codey.app.api import run_changes_response, undo_run_changes_response
from codey.app.context import AppContext
from codey.operations.task_changes import task_change_tracker
from codey.runs.ledger import RunLedgerStore
from codey.runs.task_diff import load_task_changes, record_task_changes
from codey.storage.managed_outputs import ManagedOutputStore
from codey.workspace import change_tracker
from codey.workspace.changes import ChangeTracker
from codey.workspace.paths import read_text_or_none
from codey.workspace.task_changes import collect_task_changes


def test_undo_checks_owner_and_reverses_only_the_selected_run(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = project / "shared.txt"
    source.write_text("original", encoding="utf-8")
    created = project / "new.txt"
    with closing(AppContext(tmp_path / "state", start_scheduler=False)) as ctx:
        assert ctx.run_ledgers is not None
        for run, content in (("one", "first"), ("two", "second")):
            ctx.durable.admit_input(conversation_id="owner", request_id=run, task_id=run, text=run, provider_snapshot={})
            ctx.durable.claim_task(run)
            ctx.durable.start_task(run, state={"project": str(project)})
            tracker = task_change_tracker(ctx, project, run, source_id="one" if run == "two" else "")
            tracker.capture_before(source.name)
            source.write_text(content, encoding="utf-8")
            tracker.capture_after(source.name)
            if run == "two":
                tracker.capture_before(created.name)
                created.write_text("new", encoding="utf-8")
                tracker.capture_after(created.name)
            writer = ctx.run_ledgers.open(run_id=run, session_id="owner", project=project, task=run,
                                          provider="local", mode="project")
            record_task_changes(writer, ctx.managed_outputs, collect_task_changes(project, tracker),
                                checks_passed=True, receipt={})
            ctx.durable.complete_task(run, "done", state=dict(ctx.durable.task(run).state))

        _, data = run_changes_response(ctx, {"session_id": ["owner"], "run_id": ["two"]})
        assert data["undo_available"] is True
        assert undo_run_changes_response(ctx, {"session_id": "other", "run_id": "two"})[0] == 404
        status, data = undo_run_changes_response(ctx, {"session_id": "owner", "run_id": "one"})
        assert status == 409 and data["conflicts"] == [source.name]
        assert source.read_text(encoding="utf-8") == "second"

        scope: dict[str, object] = {"session_id": "owner", "run_id": "two"}
        assert undo_run_changes_response(ctx, {**scope, "preview": True}) == (
            200, {"ok": True, "files": [created.name, source.name]},
        )
        source.write_text("user edit after preview", encoding="utf-8")
        assert undo_run_changes_response(ctx, scope)[0] == 409
        assert created.exists()
        source.write_text("second", encoding="utf-8")
        assert undo_run_changes_response(ctx, scope)[0] == 200
        assert source.read_text(encoding="utf-8") == "first"
        assert not created.exists()
        assert undo_run_changes_response(ctx, {**scope, "preview": True}) == (200, {"ok": True, "files": []})


def test_historical_task_diff_uses_first_write_and_run_identity(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    state = tmp_path / "state"
    ledgers = RunLedgerStore(state)
    outputs = ManagedOutputStore(state)
    source = project / "shared.html"
    source.write_text("base\n", encoding="utf-8")
    (project / "user.txt").write_text("dirty\n", encoding="utf-8")

    for run, after in (("a", "first"), ("b", "second")):
        tracker = ChangeTracker(project)
        tracker.capture_before(source.name)
        source.write_text(after + "\n", encoding="utf-8")
        tracker.capture_after(source.name)
        writer = ledgers.open(run_id=run, session_id="owner", project=project, task=run, provider="local", mode="project")
        record_task_changes(writer, outputs, collect_task_changes(project, tracker), checks_passed=True, receipt={})

    a = load_task_changes(RunLedgerStore(state), ManagedOutputStore(state), "owner", "a")
    b = load_task_changes(ledgers, outputs, "owner", "b")
    assert a is not None and b is not None
    first, second = a["diff"], b["diff"]
    assert isinstance(first, str) and isinstance(second, str)
    assert "-base\n+first" in first and "second" not in first and "user.txt" not in first
    assert "-first\n+second" in second and "user.txt" not in second
    assert a["html_previews"] == {"shared.html": "first\n"}
    assert b["html_previews"] == {"shared.html": "second\n"}
    assert load_task_changes(ledgers, outputs, "other", "a") is None
    assert load_task_changes(ledgers, outputs, "owner", "a~") is None


def test_task_diff_reads_each_output_once(tmp_path: Path) -> None:
    source = tmp_path / "shared.txt"
    source.write_text("base\n", encoding="utf-8")
    tracker = ChangeTracker(tmp_path)
    tracker.capture_before(source.name)
    source.write_text("settled\n", encoding="utf-8")
    tracker.capture_after(source.name)

    def read_and_write(path: Path, *, max_bytes: int) -> str | None:
        text = read_text_or_none(path, max_bytes=max_bytes)
        source.write_text("outside\n", encoding="utf-8")
        return text

    with patch.object(change_tracker, "_read_text_or_none", side_effect=read_and_write) as observed:
        result = collect_task_changes(tmp_path, tracker)
        assert observed.call_count == 1
    body = result["diff"]
    assert isinstance(body, str) and "+settled" in body and "outside" not in body
    unavailable = collect_task_changes(tmp_path, tracker)
    assert unavailable["ok"] is False and unavailable["diff"] == ""


def test_task_diff_keeps_html_with_unretained_dependencies_as_diff(tmp_path: Path) -> None:
    tracker = ChangeTracker(tmp_path)
    path = tmp_path / "page.html"
    tracker.capture_before(path.name)
    path.write_text('<link rel="stylesheet" href="./main.css"><h1>saved</h1>', encoding="utf-8")
    tracker.capture_after(path.name)
    result = collect_task_changes(tmp_path, tracker)
    assert result["changed_count"] == 1
    assert "html_previews" not in result


def test_task_diff_counts_content_that_begins_with_diff_markers(tmp_path: Path) -> None:
    path = tmp_path / "source.txt"
    path.write_text("--old\n", encoding="utf-8")
    tracker = ChangeTracker(tmp_path)
    tracker.capture_before(path.name)
    path.write_text("++new\n", encoding="utf-8")
    tracker.capture_after(path.name)
    assert collect_task_changes(tmp_path, tracker)["files"] == [
        {"path": path.name, "status": "M", "additions": 1, "deletions": 1},
    ]


@pytest.mark.parametrize(("before", "after", "hunk"), [
    ("line", "line\n", "-line\n\\ No newline at end of file\n+line"),
    ("line\n", "line", "-line\n+line\n\\ No newline at end of file"),
])
def test_task_diff_retains_end_of_file_newline_change(tmp_path: Path, before: str, after: str, hunk: str) -> None:
    path = tmp_path / "source.txt"
    path.write_bytes(before.encode())
    tracker = ChangeTracker(tmp_path)
    tracker.capture_before(path.name)
    path.write_bytes(after.encode())
    tracker.capture_after(path.name)

    result = collect_task_changes(tmp_path, tracker)
    body = result["diff"]
    assert isinstance(body, str) and body.endswith("@@ -1 +1 @@\n" + hunk)
    assert result["files"] == [{"path": path.name, "status": "M", "additions": 1, "deletions": 1}]
