"""Create isolated source copies for adapter self-repair.

The sandbox materializes exactly the repair pipeline's input surface: the
``codey`` package (the mutable adapter layer plus everything the override
installer copies and the provider unit tests import), ``pyproject.toml``
(ruff config parity for static checks), and explicitly requested read-only
reference files. Everything else in the repo -- docs, reference projects,
fixtures, tooling -- never enters a sandbox.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path


_COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")


@dataclass(frozen=True)
class RepairSandbox:
    baseline_root: Path
    candidate_root: Path
    temp_root: Path

    def cleanup(self) -> None:
        shutil.rmtree(self.temp_root, ignore_errors=True)


def create_repair_sandbox(
    source_root: str | Path | None = None,
    *,
    extra_files: tuple[str, ...] = (),
) -> RepairSandbox:
    source = Path(source_root) if source_root is not None else Path(__file__).resolve().parents[1]
    source = source.resolve()
    temp_root = Path(tempfile.mkdtemp(prefix="codey-adapter-repair-"))
    baseline = temp_root / "baseline"
    candidate = temp_root / "candidate"
    for destination in (baseline, candidate):
        _materialize_repair_surface(source, destination, extra_files)
    return RepairSandbox(
        baseline_root=baseline,
        candidate_root=candidate,
        temp_root=temp_root,
    )


def _materialize_repair_surface(source: Path, destination: Path, extra_files: tuple[str, ...]) -> None:
    shutil.copytree(source / "codey", destination / "codey", ignore=_COPY_IGNORE)
    config = source / "pyproject.toml"
    if config.is_file():
        shutil.copy2(config, destination / "pyproject.toml")
    for rel in extra_files:
        rel_path = Path(rel.replace("\\", "/"))
        target = destination / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel_path, target)
