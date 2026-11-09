"""Small release boundary fixtures: never invoke Git export, pip, a builder or service."""
from __future__ import annotations

import io
import json
import os
import sys
import tarfile
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import Mock

import pytest

from tools import release_package_check as release

COMMIT = "a" * 40
SOURCE_FILES = {"tools/build_runtime.py": b"# backend", "tools/publish_runtime.py": b"# inventory",
                "codey/runtime.json": b"{}", "frontend/src/desktop_splash.css": b"body{}",
                "tests/test_desktop_startup.py": b"# source fixture"}


def write_archive(path: Path, files: dict[str, bytes], *, prefix: str = "") -> None:
    with tarfile.open(path, "w:gz" if path.name.endswith(".gz") else "w") as archive:
        for name, content in files.items():
            member = tarfile.TarInfo(prefix + name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...], Path]] = []
        self.tree = "".join(f"100644 blob {'b' * 40} {len(data)}\t{name}\0" for name, data in SOURCE_FILES.items())
        self.fail_at = ""
        self.probe_output = '{"cold_start": "ready"}'

    def __call__(self, stage: str, args: Sequence[str], *, cwd: Path, timeout: float) -> str:
        self.calls.append((stage, tuple(args), cwd))
        assert timeout > 0
        if stage == self.fail_at:
            raise RuntimeError("fixture stage failure")
        if stage == "resolve-commit":
            return COMMIT + "\n"
        if stage == "source-budget":
            return self.tree
        if stage == "export-source":
            output = next(value.removeprefix("--output=") for value in args if value.startswith("--output="))
            write_archive(Path(output), SOURCE_FILES)
        elif stage == "build-sdist":
            assert (cwd / "tools/build_runtime.py").read_bytes() == b"# backend"
            write_archive(Path(args[-1]) / "codey-fixture.tar.gz", SOURCE_FILES, prefix="codey-fixture/")
        elif stage == "build-wheel":
            Path(args[-1], "codey-fixture.whl").write_bytes(b"fixture wheel, not an actual build")
        elif stage == "installed-runtime":
            return self.probe_output
        return ""


def test_build_uses_one_committed_export_and_one_sdist_to_wheel_chain(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "untracked-result.json").write_text("leave the working tree alone")
    work = tmp_path / "release"
    runner = RecordingRunner()

    report = release.build_release("release-tag", work, repository=repository, runner=runner)

    stages = [stage for stage, _, _ in runner.calls]
    assert stages == ["resolve-commit", "source-budget", "export-source", "build-sdist", "build-wheel"]
    assert runner.calls[0][1][-1] == "release-tag^{commit}"
    assert runner.calls[1][1][-1] == runner.calls[2][1][-1] == COMMIT
    assert runner.calls[3][2] == work / "source"
    wheel_command = runner.calls[4][1]
    assert wheel_command[4] == report["sdist"]
    assert runner.calls[4][2] == work
    assert not (work / "source/untracked-result.json").exists()
    assert not (repository / "build").exists()
    assert not (repository / "codey.egg-info").exists()
    assert (repository / "untracked-result.json").read_text() == "leave the working tree alone"
    assert report["commit"] == COMMIT and report["source_files"] == len(SOURCE_FILES)
    assert json.loads((work / "build-report.json").read_text()) == report


@pytest.mark.parametrize("tree", [
    f"120000 blob {'b' * 40} 8\tlink\0",
    f"160000 commit {'b' * 40} -\tsubmodule\0",
    f"100644 blob {'b' * 40} {release.MAX_SOURCE_BYTES + 1}\tlarge.bin\0",
    "",
])
def test_invalid_or_oversized_tree_stops_before_export(tmp_path: Path, tree: str) -> None:
    runner = RecordingRunner()
    runner.tree = tree
    with pytest.raises(ValueError):
        release.build_release(COMMIT, tmp_path / "release", repository=tmp_path / "repository", runner=runner)
    assert [stage for stage, _, _ in runner.calls] == ["resolve-commit", "source-budget"]


def test_file_count_budget_is_checked_before_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(release, "MAX_SOURCE_FILES", 1)
    runner = RecordingRunner()
    with pytest.raises(ValueError, match="file-count"):
        release.build_release(COMMIT, tmp_path / "release", repository=tmp_path / "repository", runner=runner)
    assert [stage for stage, _, _ in runner.calls] == ["resolve-commit", "source-budget"]


def test_work_directory_inside_checkout_is_rejected_before_commands(tmp_path: Path) -> None:
    runner = RecordingRunner()
    with pytest.raises(ValueError, match="outside"):
        release.build_release(COMMIT, tmp_path / "release", repository=tmp_path, runner=runner)
    assert runner.calls == []
    assert not (tmp_path / "release").exists()


def test_existing_work_directory_is_not_overwritten(tmp_path: Path) -> None:
    work = tmp_path / "release"
    work.mkdir()
    (work / "keep.txt").write_text("keep")
    runner = RecordingRunner()
    with pytest.raises(FileExistsError):
        release.build_release(COMMIT, work, repository=tmp_path / "repository", runner=runner)
    assert runner.calls == []
    assert (work / "keep.txt").read_text() == "keep"


def test_failed_sdist_does_not_start_wheel_or_report_success(tmp_path: Path) -> None:
    runner = RecordingRunner()
    runner.fail_at = "build-sdist"
    work = tmp_path / "release"
    with pytest.raises(RuntimeError, match="fixture stage failure"):
        release.build_release(COMMIT, work, repository=tmp_path / "repository", runner=runner)
    assert runner.calls[-1][0] == "build-sdist"
    assert not (work / "build-report.json").exists()


def test_check_consumes_existing_artifacts_without_building(tmp_path: Path) -> None:
    sdist, wheel = tmp_path / "codey.tar.gz", tmp_path / "codey.whl"
    write_archive(sdist, SOURCE_FILES, prefix="codey/")
    wheel.write_bytes(b"fixture wheel")
    work = tmp_path / "check"
    runner = RecordingRunner()

    report = release.check_release(wheel, sdist, work, runner=runner)

    assert [stage for stage, _, _ in runner.calls] == ["create-environment", "install-wheel", "installed-runtime"]
    assert all(cwd == work for _, _, cwd in runner.calls)
    assert runner.calls[1][1][-1] == str(wheel)
    assert "--ignore-installed" in runner.calls[1][1]  # Never install over the borrowed global environment.
    assert runner.calls[2][1][1:3] == ("-I", str(work / "installed_runtime_probe.py"))
    assert (work / "installed_runtime_probe.py").read_bytes() == Path(release.__file__).with_name("installed_runtime_probe.py").read_bytes()
    assert json.loads((work / "check-report.json").read_text()) == report


def test_source_archive_missing_backend_stops_before_installation(tmp_path: Path) -> None:
    sdist, wheel = tmp_path / "codey.tar.gz", tmp_path / "codey.whl"
    write_archive(sdist, {"codey/__init__.py": b""}, prefix="codey/")
    wheel.touch()
    runner = RecordingRunner()
    with pytest.raises(ValueError, match="required build inputs"):
        release.check_release(wheel, sdist, tmp_path / "check", runner=runner)
    assert runner.calls == []


def test_probe_without_success_cannot_produce_a_pass_report(tmp_path: Path) -> None:
    sdist, wheel = tmp_path / "codey.tar.gz", tmp_path / "codey.whl"
    write_archive(sdist, SOURCE_FILES, prefix="codey/")
    wheel.touch()
    runner = RecordingRunner()
    runner.probe_output = "not completed"
    with pytest.raises(RuntimeError, match="did not report success"):
        release.check_release(wheel, sdist, tmp_path / "check", runner=runner)
    assert not (tmp_path / "check/check-report.json").exists()


def test_child_temporaries_are_explicit_and_never_inherited_from_ramdisk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEMP", "R:\\old-temp")
    monkeypatch.setenv("PYTHONPATH", "checkout")
    monkeypatch.setenv("PYTHONHOME", "old-python")
    env = release.command_environment(tmp_path)
    assert {env[name] for name in ("TEMP", "TMP", "TMPDIR")} == {str(tmp_path / "tmp")}
    assert "PYTHONPATH" not in env and "PYTHONHOME" not in env
    assert env["PIP_NO_INDEX"] == env["PIP_NO_CACHE_DIR"] == "1"
    assert os.environ["TEMP"] == "R:\\old-temp"


@pytest.mark.parametrize("failure", [None, RuntimeError("wait failed"), KeyboardInterrupt()])
def test_command_cleans_owned_process_tree_and_writes_live_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException | None,
) -> None:
    process, owner = Mock(), Mock()
    monkeypatch.setattr(release.cancellation, "start_process", Mock(return_value=(process, owner)))
    terminate = Mock()
    monkeypatch.setattr(release.cancellation, "terminate_process_tree", terminate)

    def wait(*args: object, observer: release._StageLog, **kwargs: object) -> object:
        observer.offer("stdout", b"stage output\n")
        assert (tmp_path / "fixture.log").read_bytes() == b"stage output\n"
        if failure is not None:
            raise failure
        return release.cancellation.CapturedProcess(args=["fixture"], returncode=0, stdout="stage output\n", stderr="",
            stdout_bytes=13, stderr_bytes=0, stdout_truncated=False, stderr_truncated=False)

    monkeypatch.setattr(release.cancellation, "wait_process", wait)
    commands = release.Commands(tmp_path)
    if failure is not None:
        with pytest.raises(type(failure)):
            commands("fixture", [sys.executable, "fixture"], cwd=tmp_path, timeout=1)
    else:
        assert commands("fixture", [sys.executable, "fixture"], cwd=tmp_path, timeout=1) == "stage output\n"
    terminate.assert_called_once_with(process, owner)
    owner.close.assert_called_once_with()


def test_cli_requires_explicit_action_and_artifact_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    start = Mock(side_effect=AssertionError("CLI usage must not start a subprocess"))
    monkeypatch.setattr(release.cancellation, "start_process", start)
    for argv in ([], ["build"], ["check"]):
        with pytest.raises(SystemExit) as error:
            release.main(argv)
        assert error.value.code == 2
    start.assert_not_called()
    assert not (release.ROOT / "tests/test_installed_runtime.py").exists()
