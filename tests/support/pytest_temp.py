"""Opt-in, process-local temporary storage for pytest and its children."""
from __future__ import annotations

import atexit
import json
import os
import shutil
import tempfile
import tomllib
import warnings
from dataclasses import dataclass
from pathlib import Path

_RUN_ENV = "CODEY_PYTEST_RAMDISK_RUN"
_TEMP_ENV = ("TEMP", "TMP", "TMPDIR")
_MIN_FREE_BYTES = 16 * 1024 * 1024
_RUNS: dict[Path, TempRun] = {}


@dataclass
class TempRun:
    root: Path
    owner_pid: int
    finished: bool = False

    @property
    def temp(self) -> Path:
        return self.root / "tmp"

    @property
    def retained(self) -> bool:
        return not self.finished or (self.root / ".failed").exists()

    def finish(self, exitstatus: int) -> None:
        if exitstatus:
            # Failures in a nested pytest process must also preserve the run.
            (self.root / ".failed").touch()
        self.finished = True

    def preserve_home(self, home: Path) -> bool:
        return home.is_relative_to(self.root) and self.retained

    def cleanup(self) -> None:
        if os.getpid() != self.owner_pid or self.retained:
            return
        try:
            shutil.rmtree(self.root)
        except OSError as exc:
            warnings.warn(f"Could not remove pytest RAM-disk directory {self.root}: {exc}", RuntimeWarning, stacklevel=2)


def _inherited_run(project: Path, base: Path) -> TempRun | None:
    try:
        data = json.loads(os.environ.get(_RUN_ENV, "null"))
        if not isinstance(data, dict) or data.get("project") != str(project):
            return None
        root = Path(data["root"]).resolve()
        owner = data["owner_pid"]
        if root.parent == base and root.name.startswith("run-") and (root / "tmp").is_dir() and type(owner) is int:
            return TempRun(root, owner)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def bootstrap(project_root: Path) -> TempRun | None:
    """Initialize before the isolated home and test modules are imported.

    No local configuration means no changes, including in a clean CI checkout.
    GitHub Actions ignores the configuration even if it was accidentally copied.
    """
    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        return None
    project = project_root.resolve()
    if project in _RUNS:
        return _RUNS[project]
    config = project / ".pytest-local.toml"
    allocated: Path | None = None
    try:
        if not config.exists():
            return None
        with config.open("rb") as stream:
            settings = tomllib.load(stream)
        configured = settings["ramdisk"]["root"]
        if not isinstance(configured, str) or not configured.strip():
            raise ValueError("ramdisk.root must be a nonempty absolute path")
        base = Path(configured)
        if not base.is_absolute():
            raise ValueError("ramdisk.root must be an absolute path")
        if not Path(base.anchor).is_dir():
            raise OSError(f"RAM-disk drive is unavailable: {base.anchor}")
        base = base.resolve()
        base.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(base).free < _MIN_FREE_BYTES:
            raise OSError("RAM disk has less than 16 MiB free")
        run = _inherited_run(project, base)
        if run is None:
            allocated = Path(tempfile.mkdtemp(prefix="run-", dir=base)).resolve()
            run = TempRun(allocated, os.getpid())
            run.temp.mkdir()
        # Probe actual writes before changing any environment or cached default.
        with tempfile.TemporaryFile(dir=run.temp) as probe:
            probe.write(b"pytest")
            probe.flush()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if allocated is not None:
            shutil.rmtree(allocated, ignore_errors=True)
        warnings.warn(
            f"Pytest RAM disk disabled; keeping the default temporary directory: {exc}",
            RuntimeWarning,
            stacklevel=2,
        )
        return None
    for name in _TEMP_ENV:
        os.environ[name] = str(run.temp)
    tempfile.tempdir = str(run.temp)
    os.environ[_RUN_ENV] = json.dumps({"project": str(project), "root": str(run.root), "owner_pid": run.owner_pid})
    _RUNS[project] = run
    if allocated is not None:
        # Registered before the isolated-home and pytest cleanups; runs last.
        atexit.register(run.cleanup)
    return run
