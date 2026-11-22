"""Persisted facts must remain safe, project-relative execution observations."""

from pathlib import Path

from codey.runs.work_checkpoint import WorkCheckpoint, WorkCheckpointStore
from codey.runtime.observe.execution_evidence import CheckEvidence
from codey.storage.local_store import write_json_atomic
from codey.utils.change_paths import safe_change_path
from codey.workspace.facts import ProjectFactsStore

FINGERPRINT = "sha256:" + "1" * 64


def test_malformed_command_fields_do_not_break_valid_cached_facts(tmp_path: Path) -> None:
    store = ProjectFactsStore(tmp_path / "state")
    project = tmp_path / "project"
    valid = {"command": "python -m unittest", "cwd": "."}
    malformed: tuple[dict[str, object], ...] = (
        {"command": 42, "cwd": "."},
        {"command": "python -m unittest", "cwd": 42},
        {"command": ["python", "-m", "unittest"], "cwd": "."},
        {"command": "python -m unittest", "cwd": ["src"]},
    )
    for row in malformed:
        write_json_atomic(store.path_for(project), {"schema_version": 1, "commands": [row, valid]})

        facts = store.load(project)

        assert tuple(item.command for item in facts.commands) == ("python -m unittest",)
        assert facts.commands[0].cwd == "."


def test_non_array_fact_collections_are_ignored_without_crashing(tmp_path: Path) -> None:
    store = ProjectFactsStore(tmp_path / "state")
    project = tmp_path / "project"
    for key in ("commands", "successful_changes"):
        for invalid in (42, True, "not an array", {"unexpected": "object"}):
            payload: dict[str, object] = {"schema_version": 1, key: invalid}
            write_json_atomic(store.path_for(project), payload)

            assert store.render(project) == ""


def test_project_facts_reject_windows_rooted_or_drive_relative_cwd(tmp_path: Path) -> None:
    store = ProjectFactsStore(tmp_path / "state")
    for cwd in ("C:/outside", "C:outside", "C:", "C:\\outside", "../outside", "/outside"):
        assert not store.record_success(tmp_path / "project", cwd, "python -m unittest"), cwd


def test_successful_change_does_not_admit_windows_absolute_files_or_checks(tmp_path: Path) -> None:
    store = ProjectFactsStore(tmp_path / "state")
    project = tmp_path / "project"
    for path in ("C:/outside/app.py", "C:outside.py", "C:\\outside\\app.py"):
        assert not store.record_successful_change(
            project, task="Update source", files=[path], checks=[CheckEvidence("python -m unittest", ".")],
        ), path
    assert not store.record_successful_change(
        project, task="Update source", files=["app.py"], checks=[CheckEvidence("python -m unittest", "C:/outside")],
    )


def test_checkpoint_does_not_relabel_invalid_check_cwd_as_project_root(tmp_path: Path) -> None:
    store = WorkCheckpointStore(tmp_path / "state")
    item = WorkCheckpoint(run_id="r", session_id="s", project=str(tmp_path / "project"), original_task="Update source")
    for cwd in ("../outside", "/outside", "C:/outside", "C:outside"):
        updated = store.record_run(
            item, command="python -m unittest", cwd=cwd, ok=True,
            workspace_revision=1, workspace_fingerprint=FINGERPRINT,
        )

        assert updated.successful_checks_after_last_change == (), cwd


def test_loaded_checkpoint_rejects_windows_paths_in_files_and_checks(tmp_path: Path) -> None:
    store = WorkCheckpointStore(tmp_path / "state")
    payload: dict[str, object] = {
        "schema_version": 1, "run_id": "r", "session_id": "s", "status": "working",
        "project": str(tmp_path / "project"),
        "changed_files": [
            {"path": "C:/outside/app.py", "after_hash": "missing"},
            {"path": "src/app.py", "after_hash": "missing"},
        ],
        "successful_checks_after_last_change": [
            {"command": "python -m unittest", "cwd": "C:outside", "workspace_revision": 1, "workspace_fingerprint": FINGERPRINT},
            {"command": "python -m unittest", "cwd": "src", "workspace_revision": 1, "workspace_fingerprint": FINGERPRINT},
        ],
    }
    write_json_atomic(store.path_for("s"), payload)

    item = store.load("s")

    assert item is not None
    assert tuple(row.path for row in item.changed_files) == ("src/app.py",)
    assert tuple(row.cwd for row in item.successful_checks_after_last_change) == ("src",)


def test_checkpoint_does_not_expand_a_string_into_unavailable_file_names(tmp_path: Path) -> None:
    store = WorkCheckpointStore(tmp_path / "state")
    payload: dict[str, object] = {
        "schema_version": 1, "run_id": "r", "session_id": "s", "status": "working",
        "project": str(tmp_path / "project"), "hash_unavailable_files": "src/app.py",
    }
    write_json_atomic(store.path_for("s"), payload)

    assert store.load("s") is None


def test_relative_change_paths_reject_embedded_null_bytes() -> None:
    for path in ("src/\x00app.py", "\x00"):
        assert safe_change_path(path) == ""
