from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ci_declares_full_tree_mypy_gate() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements-ci.txt").read_text(encoding="utf-8")
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "mypy==" in requirements
    assert "python -m mypy codey" in workflow
    assert pyproject["tool"]["codey"]["typecheck"]["gate"] == "full-tree"


def test_ci_declares_yaml_type_stubs_for_clean_mypy_install() -> None:
    requirements = (ROOT / "requirements-ci.txt").read_text(encoding="utf-8")
    assert "types-PyYAML==" in requirements
