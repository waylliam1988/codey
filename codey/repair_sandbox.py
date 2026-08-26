"""Create isolated source copies for adapter self-repair.

The sandbox materializes exactly the repair pipeline's input surface: the
``codey`` package (the mutable adapter layer plus everything the override
installer copies and the provider unit tests import), ``pyproject.toml``
(ruff config parity for static checks), and explicitly requested read-only
reference files. Everything else in the repo -- docs, reference projects,
fixtures, tooling -- never enters a sandbox.

Reference paths are an explicit parameter boundary and validate fail-closed:
empty, absolute, drive/rooted, or upward-traversing paths are rejected
before any filesystem work, every reference must name an existing file
inside the source tree, and every copy re-checks containment on both the
source and destination side. A failed materialization always removes its
temp root.
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
    if not (source / "codey").is_dir():
        # Name the missing input ourselves: raw OS errors do not carry the
        # path on every platform.
        raise FileNotFoundError(f"repair source root has no codey package: {source}")
    reference_files = tuple(_validated_reference(source, rel) for rel in extra_files)
    temp_root = Path(tempfile.mkdtemp(prefix="codey-adapter-repair-"))
    try:
        baseline = temp_root / "baseline"
        candidate = temp_root / "candidate"
        for destination in (baseline, candidate):
            _materialize_repair_surface(source, destination, reference_files)
    except BaseException:
        # A half-materialized sandbox must never leak its temp root.
        shutil.rmtree(temp_root, ignore_errors=True)
        raise
    return RepairSandbox(
        baseline_root=baseline,
        candidate_root=candidate,
        temp_root=temp_root,
    )


def _validated_reference(source: Path, rel: str) -> Path:
    text = str(rel or "").strip()
    if not text:
        raise ValueError("repair reference file path is empty")
    rel_path = Path(text.replace("\\", "/"))
    if rel_path.is_absolute() or rel_path.drive or rel_path.root:
        raise ValueError(f"repair reference file must be relative: {rel}")
    if ".." in rel_path.parts:
        raise ValueError(f"repair reference file must not traverse upward: {rel}")
    resolved_source = (source / rel_path).resolve()
    if source not in resolved_source.parents:
        raise ValueError(f"repair reference file escapes the source root: {rel}")
    if not resolved_source.is_file():
        # Name the missing input ourselves: raw OS errors do not carry the
        # path on every platform.
        raise FileNotFoundError(f"repair reference file is missing: {text}")
    return resolved_source


def _materialize_repair_surface(source: Path, destination: Path, reference_files: tuple[Path, ...]) -> None:
    shutil.copytree(source / "codey", destination / "codey", ignore=_COPY_IGNORE)
    config = source / "pyproject.toml"
    if config.is_file():
        shutil.copy2(config, destination / "pyproject.toml")
    resolved_destination = destination.resolve()
    for resolved_source in reference_files:
        target = destination / resolved_source.relative_to(source)
        if resolved_destination not in target.resolve().parents:
            raise ValueError(f"repair reference file escapes the sandbox root: {resolved_source.name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(resolved_source, target)
