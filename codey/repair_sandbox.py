"""Create isolated source copies for adapter self-repair."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path


EXCLUDED_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
}


@dataclass(frozen=True)
class RepairSandbox:
    baseline_root: Path
    candidate_root: Path
    temp_root: Path

    def cleanup(self) -> None:
        shutil.rmtree(self.temp_root, ignore_errors=True)


def create_repair_sandbox(source_root: str | Path | None = None) -> RepairSandbox:
    source = Path(source_root) if source_root is not None else Path(__file__).resolve().parents[1]
    source = source.resolve()
    temp_root = Path(tempfile.mkdtemp(prefix="codey-adapter-repair-"))
    baseline = temp_root / "baseline"
    candidate = temp_root / "candidate"
    _copy_source(source, baseline)
    _copy_source(source, candidate)
    return RepairSandbox(
        baseline_root=baseline,
        candidate_root=candidate,
        temp_root=temp_root,
    )


def _copy_source(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=lambda _dir, names: [name for name in names if name in EXCLUDED_DIRS],
    )
