"""Read-only semantic observations bind UTF-16 locations and original bytes."""
from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from codey.toolchain import semantic_definition as semantic
from codey.types.json_value import JsonValue
from codey.workspace.paths import content_hash


@pytest.mark.parametrize("changed", [False, True])
def test_definition_uses_utf16_and_rejects_a_changed_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: bool,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = project / "example.py"
    text = 'label = "😀"; found = SERVICE_LIMIT\r\n'
    path.write_bytes(text.encode("utf-8"))
    target = project / "service.py"
    target.write_text("SERVICE_LIMIT = 12\n", encoding="utf-8")
    monkeypatch.setattr(semantic, "pyright_server", lambda: sys.executable)

    def lookup(argv: Sequence[str], root: Path, document: Path, source: str, line: int, character: int) -> JsonValue:
        assert root == project and document == path and source == text and line == 0
        assert character == len(text[:text.index("SERVICE_LIMIT")].encode("utf-16-le")) // 2
        if changed:
            document.write_bytes(text.replace("found", "other").encode("utf-8"))
        return [{"targetUri": target.as_uri(), "targetSelectionRange": {"start": {"line": 0, "character": 0}}},
                {"uri": (tmp_path / "outside.py").as_uri(), "range": {"start": {"line": 0, "character": 0}}}]

    monkeypatch.setattr(semantic, "_lookup", lookup)
    result = semantic.semantic_definition(project, "example.py", 1, "SERVICE_LIMIT")
    if changed:
        assert not result.ok and "changed" in result.model_text
    else:
        assert result.ok
        assert result.canonical["definitions"] == [{"path": "service.py", "line": 1, "column_utf16": 1}]
        assert result.canonical["file_version"] == "raw:" + content_hash(text)
        assert path.read_bytes() == text.encode("utf-8")


def test_definition_is_absent_without_a_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(semantic, "pyright_server", lambda: None)
    result = semantic.semantic_definition(tmp_path, "example.py", 1, "symbol")
    assert not result.ok and "requires pyright-langserver" in result.model_text
