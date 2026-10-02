from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE_MODULES = (
    "codey/operations/task_state.py",
    "codey/operations/context.py",
    "codey/operations/ghost_context.py",
    "codey/operations/prompting.py",
    "codey/app/task_submit.py",
    "codey/app/server.py",
    "codey/app/headless_runner.py",
    "codey/workspace/changes.py",
    "codey/completion/contract.py",
    "codey/runtime/log/entries.py",
    "codey/runtime/core/cancellation.py",
    "codey/operations/task_loop.py",
    "codey/operations/project_writer_phase.py",
    "codey/operations/project_completion_enforcement.py",
    "codey/research/evidence_ledger.py",
    "codey/runs/trace.py",
)


def test_ci_declares_incremental_mypy_gate() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements-ci.txt").read_text(encoding="utf-8")
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "mypy==" in requirements
    assert "python -m mypy --follow-imports=skip" in workflow
    assert pyproject["tool"]["codey"]["typecheck"]["gate"] == "incremental-core"


def test_incremental_mypy_gate_targets_typed_boundary_modules() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for module in CORE_MODULES:
        assert module in workflow
