"""Keep manual benchmark output outside normal pytest collection."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_pytest_ignores_manual_result_artifacts() -> None:
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    pytest_options = config["tool"]["pytest"]["ini_options"]

    ignored = set(pytest_options.get("norecursedirs", []))
    assert "tests/manual/results" in ignored
