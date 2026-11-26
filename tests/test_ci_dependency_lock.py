"""CI's locked install must include every declared runtime dependency."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
DEPENDENCIES = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]


@pytest.mark.parametrize("dependency", DEPENDENCIES)
def test_ci_lock_covers_runtime_dependency(dependency: str) -> None:
    required = Requirement(dependency)
    locked = {
        canonicalize_name(requirement.name): requirement
        for line in (ROOT / "requirements-ci.txt").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith(("#", " ", "\t"))
        for requirement in [Requirement(line.removesuffix(" \\"))]
    }
    name = canonicalize_name(required.name)
    assert name in locked, f"{required.name} is missing; regenerate requirements-ci.txt"
    pins = list(locked[name].specifier)
    assert len(pins) == 1 and pins[0].operator == "==", f"{required.name} must be pinned"
    assert pins[0].version in required.specifier, f"{locked[name]} does not satisfy {required}"
