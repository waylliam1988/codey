"""Canonical read/edit keys use the same expanded root as the workspace guard."""

import tempfile
from pathlib import Path
from typing import Literal

import pytest

from codey.agents.protocol import canonical_project_path
from codey.workspace.paths import safe_join


@pytest.mark.parametrize("root_kind", ["absolute", "home_alias"])
@pytest.mark.parametrize("relative,expected", [
    (".", "."), ("pkg/app.py", "pkg/app.py"), ("pkg/../pkg/app.py", "pkg/app.py"),
])
def test_canonical_paths_resolve_inside_equivalent_project_roots(
    root_kind: Literal["absolute", "home_alias"], relative: str, expected: str,
) -> None:
    with tempfile.TemporaryDirectory(prefix="codey-path-", dir=Path.home()) as directory:
        actual = Path(directory).resolve()
        (actual / "pkg").mkdir()
        (actual / "pkg" / "app.py").write_text("value = 1\n", encoding="utf-8")
        root = Path("~") / actual.name if root_kind == "home_alias" else actual

        assert safe_join(root, relative) == (actual / relative).resolve()
        assert canonical_project_path(root, relative) == expected


@pytest.mark.parametrize("root_kind", ["absolute", "home_alias"])
def test_canonical_paths_still_reject_project_escapes(
    root_kind: Literal["absolute", "home_alias"],
) -> None:
    with tempfile.TemporaryDirectory(prefix="codey-path-", dir=Path.home()) as directory:
        actual = Path(directory).resolve()
        root = Path("~") / actual.name if root_kind == "home_alias" else actual

        with pytest.raises(ValueError, match="path escapes"):
            canonical_project_path(root, "../outside.py")
