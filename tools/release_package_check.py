"""Manual release artifacts and installation checks; never part of pytest collection."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tarfile
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from codey.runtime.core import cancellation

ROOT = Path(__file__).resolve().parents[1]
MAX_SOURCE_FILES = 10_000
MAX_SOURCE_BYTES = 128 * 1024 * 1024
CAPTURE_BYTES = 4 * 1024 * 1024


class Runner(Protocol):
    def __call__(self, stage: str, args: Sequence[str], *, cwd: Path, timeout: float) -> str: ...


class _StageLog:
    def __init__(self, path: Path) -> None:
        self._stream = path.open("wb")
        self._lock = threading.Lock()

    def offer(self, channel: str, chunk: bytes) -> None:
        with self._lock:
            self._stream.write(chunk)
            self._stream.flush()

    def close(self) -> None:
        with self._lock:
            self._stream.close()


def command_environment(work: Path) -> dict[str, str]:
    env = {name: value for name, value in os.environ.items() if name not in {"PYTHONPATH", "PYTHONHOME"}}
    env.update(TEMP=str(work / "tmp"), TMP=str(work / "tmp"), TMPDIR=str(work / "tmp"),
               PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1", PIP_NO_INDEX="1",
               PIP_NO_CACHE_DIR="1", PYTHONNOUSERSITE="1")
    return env


class Commands:
    def __init__(self, work: Path) -> None:
        self.work = work
        self.env = command_environment(work)

    def __call__(self, stage: str, args: Sequence[str], *, cwd: Path, timeout: float) -> str:
        log_path = self.work / f"{stage}.log"
        print(f"{stage}: started; log={log_path}", flush=True)
        observer = _StageLog(log_path)
        try:
            process, owner = cancellation.start_process(args, cwd=cwd, env=self.env)
            try:
                result = cancellation.wait_process(process, owner, args, timeout,
                    capture_limit_bytes=CAPTURE_BYTES, observer=observer)
            finally:
                # Successful parents may also leave service descendants.
                cancellation.terminate_process_tree(process, owner)
                if owner is not None:
                    owner.close()
        finally:
            observer.close()
        if result.returncode:
            raise RuntimeError(f"{stage} failed ({result.returncode}); inspect {log_path}")
        if result.stdout_truncated:
            raise RuntimeError(f"{stage} output exceeds capture budget; inspect {log_path}")
        print(f"{stage}: passed", flush=True)
        return result.stdout


def _new_work_directory(path: Path, repository: Path) -> Path:
    work = path.resolve()
    if work.is_relative_to(repository.resolve()):
        raise ValueError("Release work directory must be outside the source checkout")
    work.mkdir(parents=True, exist_ok=False)
    (work / "tmp").mkdir()
    return work


@dataclass(frozen=True)
class SourceBudget:
    files: int
    bytes: int


def source_budget(tree: str) -> SourceBudget:
    files = total = 0
    for row in tree.split("\0"):
        if not row:
            continue
        metadata, _path = row.split("\t", 1)
        mode, kind, _identity, size = metadata.split()
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ValueError("Release export requires regular files; links and submodules are unsupported")
        files += 1
        total += int(size)
        if files > MAX_SOURCE_FILES or total > MAX_SOURCE_BYTES:
            raise ValueError("Release source exceeds file-count or byte budget; no source was exported")
    if not files:
        raise ValueError("Release source tree is empty")
    return SourceBudget(files, total)


def _one_artifact(directory: Path, pattern: str) -> Path:
    matches = list(directory.glob(pattern))
    if len(matches) != 1:
        raise ValueError(f"Expected one {pattern} artifact in {directory}, found {len(matches)}")
    return matches[0]


def check_source_archive(archive: Path) -> None:
    with tarfile.open(archive) as source:
        names = {name.split("/", 1)[1] for name in source.getnames() if "/" in name}
    required = {"tools/build_runtime.py", "tools/publish_runtime.py", "codey/runtime.json",
                "frontend/src/desktop_splash.css", "tests/test_desktop_startup.py"}
    missing = required - names
    if missing:
        raise ValueError(f"Source archive is missing required build inputs: {sorted(missing)}")


def build_release(commit: str, work_dir: Path, *, repository: Path = ROOT,
                  runner: Runner | None = None) -> dict[str, object]:
    repository = repository.resolve()
    work = _new_work_directory(work_dir, repository)
    run = runner if runner is not None else Commands(work)
    with cancellation.deadline_scope(time.monotonic() + 600):
        revision = run("resolve-commit", ["git", "rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}"],
                       cwd=repository, timeout=30).strip()
        tree = run("source-budget", ["git", "ls-tree", "-r", "-l", "-z", revision], cwd=repository, timeout=30)
        budget = source_budget(tree)
        archive = work / "source.tar"
        run("export-source", ["git", "archive", "--format=tar", f"--output={archive}", revision],
            cwd=repository, timeout=60)
        source = work / "source"
        source.mkdir()
        with tarfile.open(archive) as exported:
            exported.extractall(source, filter="data")
        dist = work / "dist"
        dist.mkdir()
        run("build-sdist", [sys.executable, "-c",
            "import sys;from tools.build_runtime import build_sdist;build_sdist(sys.argv[1])", str(dist)],
            cwd=source, timeout=90)
        sdist = _one_artifact(dist, "*.tar.gz")
        check_source_archive(sdist)
        # Building the only wheel from this sdist also proves its backend is self-contained.
        run("build-wheel", [sys.executable, "-m", "pip", "wheel", str(sdist), "--no-deps",
            "--no-build-isolation", "--no-index", "--wheel-dir", str(dist)], cwd=work, timeout=180)
        wheel = _one_artifact(dist, "*.whl")
    report: dict[str, object] = {"commit": revision, "source_files": budget.files, "source_bytes": budget.bytes,
                                "sdist": str(sdist), "wheel": str(wheel)}
    (work / "build-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def check_release(wheel: Path, sdist: Path, work_dir: Path, *, runner: Runner | None = None) -> dict[str, object]:
    wheel, sdist = wheel.resolve(strict=True), sdist.resolve(strict=True)
    check_source_archive(sdist)
    work = _new_work_directory(work_dir, ROOT)
    run = runner if runner is not None else Commands(work)
    with cancellation.deadline_scope(time.monotonic() + 360):
        environment = work / "venv"
        run("create-environment", [sys.executable, "-I", "-m", "venv", "--system-site-packages", str(environment)],
            cwd=work, timeout=90)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run("install-wheel", [str(python), "-I", "-m", "pip", "install", "--no-deps", "--no-index",
            "--ignore-installed", str(wheel)], cwd=work, timeout=60)
        probe = work / "installed_runtime_probe.py"
        shutil.copyfile(Path(__file__).with_name("installed_runtime_probe.py"), probe)
        output = run("installed-runtime", [str(python), "-I", str(probe), str(work / "state")], cwd=work, timeout=180)
        if '"cold_start": "ready"' not in output:
            raise RuntimeError("Installed probe did not report success; inspect installed-runtime.log")
    report: dict[str, object] = {"wheel": str(wheel), "sdist": str(sdist), "installed_runtime": "passed"}
    (work / "check-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


class Arguments(argparse.Namespace):
    action: str
    commit: str
    work_dir: Path
    wheel: Path
    sdist: Path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    build = actions.add_parser("build", help="Export a committed source tree and build sdist/wheel once")
    build.add_argument("--commit", required=True, help="Exact commit or explicit Git revision; excludes working-tree edits")
    build.add_argument("--work-dir", type=Path, required=True, help="New directory outside the checkout, on a chosen disk")
    check = actions.add_parser("check", help="Validate existing release artifacts; never builds a package")
    check.add_argument("--wheel", type=Path, required=True)
    check.add_argument("--sdist", type=Path, required=True)
    check.add_argument("--work-dir", type=Path, required=True, help="New directory for installation and service state")
    args = parser.parse_args(argv, namespace=Arguments())
    if args.action == "build":
        report = build_release(args.commit, args.work_dir)
    else:
        report = check_release(args.wheel, args.sdist, args.work_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
