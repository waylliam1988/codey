from __future__ import annotations

import json
from unittest import mock

import pytest

from codey.storage.local_store import StoreCorruption
from codey.toolchain.runtime import search_files
from codey.workspace.bounded_scan import BoundedScanBudget
from codey.workspace.file_index import IncrementalFileIndex


def discover(index, root, start=None, **limits):
    return index.discover(root, start or root, excluded_dirs=set(), budget=BoundedScanBudget(**limits))


def test_cold_warm_and_reopen_preserve_incomplete_discovery(tmp_path):
    project, state = tmp_path / "project", tmp_path / "state"
    project.mkdir()
    (project / "a.py").write_text("other\n")
    (project / "b.py").write_text("needle\n")
    index = IncrementalFileIndex(state)
    with mock.patch("codey.toolchain.runtime.SEARCH_MAX_SCAN_FILES", 1):
        cold = search_files(project, ".", "needle", file_index=index)
        warm = search_files(project, ".", "needle", file_index=index)
        reopened = search_files(project, ".", "needle", file_index=IncrementalFileIndex(state))
    assert cold.ok and warm.ok and reopened.ok
    assert cold.canonical == warm.canonical == reopened.canonical
    assert cold.canonical["value"]["scan_complete"] is False
    assert cold.canonical["value"]["coverage"]["budget_limited"]


@pytest.mark.parametrize("limits", [{"max_files": 1}, {"max_dirs": 1}, {"max_dir_entries": 1}])
def test_snapshot_owns_coverage_without_mutating_callers_budget(tmp_path, limits):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.py").write_text("needle")
    (tmp_path / "b.py").write_text("other")
    index = IncrementalFileIndex()
    budget = BoundedScanBudget(**limits)
    cold = index.discover(tmp_path, tmp_path, excluded_dirs=set(), budget=budget)
    warm = index.discover(tmp_path, tmp_path, excluded_dirs=set(), budget=budget)
    assert cold.coverage == warm.coverage
    assert cold.coverage.budget().limited
    assert budget == BoundedScanBudget(**limits)


def test_warm_snapshot_reuses_discovery_but_refreshes_file_metadata(tmp_path):
    source = tmp_path / "a.py"
    source.write_text("one")
    index = IncrementalFileIndex()
    first = discover(index, tmp_path)
    source.write_text("changed")
    with mock.patch("codey.workspace.file_index.iter_bounded_files", side_effect=AssertionError("walked")):
        second = discover(index, tmp_path)
    assert second.generation == first.generation
    assert second.entries[0].size == len("changed")
    assert second.coverage == first.coverage


def test_index_detects_new_files_in_previously_empty_directory(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    index = IncrementalFileIndex()
    assert not discover(index, tmp_path).entries
    (nested / "new.py").write_text("new")
    assert [e.path for e in discover(index, tmp_path).entries] == [nested / "new.py"]


@pytest.mark.parametrize("reopen", [False, True])
@pytest.mark.parametrize("change", ["create", "nested", "rename", "symlink_to_file"])
def test_membership_changes_are_detected_with_unchanged_directory_timestamps(tmp_path, monkeypatch, reopen, change):
    project, state = tmp_path / "project", tmp_path / "state"
    nested = project / "nested"
    nested.mkdir(parents=True)
    before = nested / "before.py"
    if change == "rename":
        before.write_text("needle")
    elif change == "symlink_to_file":
        target = tmp_path / "outside.py"
        target.write_text("outside")
        try:
            before.symlink_to(target)
        except OSError as exc:
            pytest.skip(f"symlink creation unavailable: {exc}")
    original = IncrementalFileIndex._directory

    def fixed_stamp(path):
        name, _mtime, device, inode = original(path)
        return name, 0, device, inode

    monkeypatch.setattr(IncrementalFileIndex, "_directory", staticmethod(fixed_stamp))
    index = IncrementalFileIndex(state)
    assert search_files(project, ".", "needle", file_index=index).ok
    if change == "nested":
        destination = nested / "new_dir" / "new.py"
        destination.parent.mkdir()
    else:
        destination = nested / ("before.py" if change == "symlink_to_file" else "new.py")
    if change == "rename":
        before.rename(destination)
    else:
        if change == "symlink_to_file":
            before.unlink()
        destination.write_text("needle")
    if reopen:
        index = IncrementalFileIndex(state)
    result = search_files(project, ".", "needle", file_index=index)
    assert result.ok
    assert [entry["path"] for entry in result.canonical["value"]["matches"]] == [destination.relative_to(project).as_posix()]
    assert result.canonical["value"]["scan_complete"] is True


def test_warm_membership_validation_obeys_the_original_scope_and_entry_cap(tmp_path, monkeypatch):
    from pathlib import Path

    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    for number in range(20):
        (left / f"{number:02}.py").write_text("needle")
    index = IncrementalFileIndex()
    cold = discover(index, tmp_path, left, max_dir_entries=2)
    observed = []
    original = Path.iterdir

    def counted(path):
        assert path == left  # Never enumerate siblings or the project root.
        for entry in original(path):
            observed.append(entry)
            yield entry

    monkeypatch.setattr(Path, "iterdir", counted)
    with mock.patch("codey.workspace.file_index.iter_bounded_files", side_effect=AssertionError("walked")):
        warm = discover(index, tmp_path, left, max_dir_entries=2)
    assert len(observed) == 3  # Two admitted entries and the bounded overflow probe.
    assert warm.generation == cold.generation
    assert warm.coverage == cold.coverage and warm.coverage.entry_limited


@pytest.mark.parametrize("reopen", [False, True])
def test_new_entry_beyond_a_full_prefix_cannot_keep_complete_coverage(tmp_path, monkeypatch, reopen):
    from pathlib import Path

    project, state = tmp_path / "project", tmp_path / "state"
    project.mkdir()
    (project / "a.py").write_text("other")
    directory, listing = IncrementalFileIndex._directory, Path.iterdir
    monkeypatch.setattr(IncrementalFileIndex, "_directory", staticmethod(
        lambda path: (path, 0, *directory(path)[2:])))
    monkeypatch.setattr(Path, "iterdir", lambda path: iter(sorted(listing(path))))
    index = IncrementalFileIndex(state)
    with mock.patch("codey.toolchain.runtime.SEARCH_MAX_DIR_ENTRIES", 1):
        before = search_files(project, ".", "needle", file_index=index)
        assert before.ok and before.canonical["value"]["scan_complete"] is True
        (project / "b.py").write_text("needle")
        if reopen:
            index = IncrementalFileIndex(state)
        after = search_files(project, ".", "needle", file_index=index)
    assert after.ok and after.canonical["value"]["scan_complete"] is False
    assert after.canonical["value"]["coverage"]["budget_limited"] is True


def test_index_keeps_scopes_and_policies_independent(tmp_path):
    for name in ("left", "right"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "a.py").write_text(name)
    index = IncrementalFileIndex()
    left = discover(index, tmp_path, tmp_path / "left")
    right = discover(index, tmp_path, tmp_path / "right")
    assert left.entries[0].path.parent.name == "left"
    assert right.entries[0].path.parent.name == "right"
    assert left.generation != right.generation


def test_corrupt_or_unwritable_index_never_bypasses_persistence(tmp_path):
    project, state = tmp_path / "project", tmp_path / "state"
    project.mkdir()
    (project / "a.py").write_text("needle")
    first = IncrementalFileIndex(state)
    assert search_files(project, ".", "needle", file_index=first).ok
    snapshot = next((state / "search_index").glob("*.json"))
    snapshot.write_text("{bad")
    outcome = search_files(project, ".", "needle", file_index=IncrementalFileIndex(state))
    assert not outcome.ok and outcome.error_code == "search_index_failed"
    with mock.patch("codey.workspace.file_index.write_json_atomic", side_effect=OSError("disk full")):
        result = search_files(project, ".", "needle", file_index=first)
        assert not result.ok and "disk full" in result.model_text


def test_snapshot_digest_covers_omissions(tmp_path):
    project, state = tmp_path / "project", tmp_path / "state"
    project.mkdir()
    (project / "a.py").write_text("needle")
    discover(IncrementalFileIndex(state), project)
    file = next((state / "search_index").glob("*.json"))
    raw = json.loads(file.read_text())
    raw["snapshot"]["coverage"]["file_limited"] = True
    file.write_text(json.dumps(raw))
    with pytest.raises(StoreCorruption, match="digest"):
        discover(IncrementalFileIndex(state), project)


def test_snapshot_format_is_part_of_cache_identity_without_reading_old_storage(tmp_path):
    import hashlib

    project, state = tmp_path / "project", tmp_path / "state"
    project.mkdir()
    source = project / "new.py"
    source.write_text("needle")
    limits = BoundedScanBudget()
    old_policy = [str(project.resolve()), str(project.resolve()), [], True,
                  limits.max_files, limits.max_dirs, limits.max_dir_entries]
    old_key = hashlib.sha256(json.dumps(old_policy, separators=(",", ":")).encode()).hexdigest()
    obsolete = state / "search_index" / f"{old_key}.json"
    obsolete.parent.mkdir(parents=True)
    obsolete.write_text("obsolete schema-2 snapshot")
    from codey.storage.local_store import read_json_strict

    observed = []

    def read_current(path):
        observed.append(path)
        assert path != obsolete  # No attempt to parse/repair an obsolete format.
        return read_json_strict(path)

    with mock.patch("codey.workspace.file_index.read_json_strict", side_effect=read_current):
        first = discover(IncrementalFileIndex(state), project)
        reopened = discover(IncrementalFileIndex(state), project)
    assert first == reopened and [entry.path for entry in first.entries] == [source]
    assert len(observed) == 2 and observed[0] == observed[1]
    assert obsolete.read_text() == "obsolete schema-2 snapshot"


def test_invalid_directory_metadata_is_corruption_even_with_valid_envelope(tmp_path):
    import hashlib
    project, state = tmp_path / 'project', tmp_path / 'state'
    project.mkdir()
    (project / 'a.py').write_text('needle')
    discover(IncrementalFileIndex(state), project)
    file = next((state / 'search_index').glob('*.json'))
    raw = json.loads(file.read_text())
    raw['snapshot']['directories'][0][1] = 'invalid'
    encoded = json.dumps(raw['snapshot'],sort_keys=True,separators=(',',':'))
    raw['sha256'] = hashlib.sha256(encoded.encode()).hexdigest()
    file.write_text(json.dumps(raw))
    with pytest.raises(StoreCorruption,match='directory metadata'):
        discover(IncrementalFileIndex(state),project)


@pytest.mark.parametrize("entry", ["delegate", "managed", "task_entry"])
def test_production_tool_sets_reopen_same_persistent_index(tmp_path, entry):
    from types import SimpleNamespace

    from codey.operations.project_completion_context import managed_tool_fns
    from codey.operations.task_entry import _entry_executors
    from codey.operations.task_execution import ExecutionDelegate
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall
    from codey.storage.managed_outputs import ManagedOutputStore

    project, state = tmp_path / "project", tmp_path / "state"
    project.mkdir()
    (project / "a.py").write_text("needle\n")
    outputs = ManagedOutputStore(state)
    deps = SimpleNamespace(persistence=SimpleNamespace(managed_outputs=outputs, execution_outputs=None))

    def execute():
        if entry == "task_entry":
            frame = SimpleNamespace(project_text=str(project), request=SimpleNamespace(session_id="s"))
            _, tool_fns, _ = _entry_executors(
                frame, SimpleNamespace(managed_outputs=outputs), TaskPolicy(frozenset({"project.read"})),
            )
        else:
            tool_fns = managed_tool_fns(deps, session_id="s", run_id="r") if entry == "managed" else None
        delegate = ExecutionDelegate(
            session=TaskSession(TaskPolicy(frozenset({"project.read"}))),
            project_path=project, tool_fns=tool_fns, managed_outputs=outputs,
        )
        return delegate.execute(ToolCall("grep", {"path": ".", "query": "needle"}))[0]

    cold = execute()
    assert cold.ok
    snapshots = list((state / "search_index").glob("*.json"))
    assert len(snapshots) == 1
    with mock.patch("codey.workspace.file_index.iter_bounded_files", side_effect=AssertionError("walked")):
        warm = execute()
    assert warm.ok and warm.canonical == cold.canonical
    snapshots[0].write_text("{bad")
    corrupt = execute()
    assert not corrupt.ok and "index" in corrupt.model_text.lower()
