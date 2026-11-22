"""Audit path rejection must use the same expanded root as filesystem reads."""

from pathlib import Path

import pytest

from codey.agents.project_audit_tools import execute_read_only_call
from codey.runtime.core.models import ToolCall
from codey.types.json_value import JsonObject


def _project_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    target = tmp_path / "actual.py"
    target.write_text("needle = 1\n", encoding="utf-8")
    link = tmp_path / "alias.py"
    tilde_root = Path("~") / "audit-project"
    original_expanduser = Path.expanduser
    original_resolve = Path.resolve
    original_is_symlink = Path.is_symlink

    # Model the filesystem boundary without requiring symlink privileges.
    def expanduser(path: Path) -> Path:
        return tmp_path if path == tilde_root else original_expanduser(path)

    def resolve(path: Path, strict: bool = False) -> Path:
        return original_resolve(target if path == link else path, strict)

    def is_symlink(path: Path) -> bool:
        return path == link or original_is_symlink(path)

    monkeypatch.setattr(Path, "expanduser", expanduser)
    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(Path, "is_symlink", is_symlink)
    return tmp_path, tilde_root


@pytest.mark.parametrize("tool", ["search", "references"])
@pytest.mark.parametrize("unexpanded", [False, True])
def test_audit_rejects_symlink_paths_for_expanded_and_tilde_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, unexpanded: bool,
) -> None:
    expanded_root, tilde_root = _project_paths(tmp_path, monkeypatch)
    arguments: JsonObject = {"path": "alias.py"}
    arguments["query" if tool == "search" else "symbol"] = "needle"

    outcome = execute_read_only_call(tilde_root if unexpanded else expanded_root, ToolCall(tool, arguments))

    assert not outcome.ok
    assert "symlink" in outcome.model_text
    assert "needle = 1" not in outcome.model_text


@pytest.mark.parametrize("tool", ["search", "references"])
def test_audit_finds_regular_file_text_with_a_tilde_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str,
) -> None:
    _expanded_root, tilde_root = _project_paths(tmp_path, monkeypatch)
    arguments: JsonObject = {"path": "actual.py"}
    arguments["query" if tool == "search" else "symbol"] = "needle"

    outcome = execute_read_only_call(tilde_root, ToolCall(tool, arguments))

    assert outcome.ok
    assert "actual.py:1:" in outcome.model_text
    assert "needle = 1" in outcome.model_text
