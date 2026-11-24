from __future__ import annotations

from pathlib import Path

import pytest

from codey.toolchain.runtime import EditBlock, edit_file, edit_file_at_version

OLD = "  const résultat = value + 1;\n  return résultat;"
NEW = OLD.replace("value + 1", "Math.max(0, value)")
SOURCE = "function positive(value) {\n    const résultat = value + 1;\n    return résultat;\n}\n"


@pytest.mark.parametrize("suffix", ["ts", "js"])
@pytest.mark.parametrize("margin", ["  ", "      "])
def test_js_ts_uniform_alignment_preserves_crlf_and_unicode(tmp_path: Path, suffix: str, margin: str) -> None:
    path = tmp_path / f"sample.{suffix}"
    path.write_bytes(SOURCE.replace("\n", "\r\n").encode())
    old = "\n".join(margin + line.lstrip() for line in OLD.splitlines())
    new = old.replace("value + 1", "Math.max(0, value)")
    result = edit_file(tmp_path, path.name, [EditBlock(old, new)])
    assert result.ok and result.changed and result.audit["indentation_adjusted"] == 1
    assert path.read_bytes() == SOURCE.replace("value + 1", "Math.max(0, value)").replace("\n", "\r\n").encode()


def test_ambiguity_and_stale_versions_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "sample.ts"
    original = SOURCE + SOURCE.replace("positive", "other")
    path.write_bytes(original.encode())
    blocks = [EditBlock(OLD, NEW)]
    result = edit_file(tmp_path, path.name, blocks)
    assert not result.ok and "2, 6" in result.model_text
    stale = edit_file_at_version(tmp_path, path.name, blocks, version="sha256:stale")
    assert stale.error_code == "stale_read_reference"
    assert path.read_bytes() == original.encode()


@pytest.mark.parametrize("literal", [
    "`begin\n    const résultat = value + 1;\n    return résultat;\nend`",
    "<p>begin\n    const résultat = value + 1;\n    return résultat;\nend</p>",
    "null; /* begin\n    const résultat = value + 1;\n    return résultat;\nend */",
])
def test_alignment_refuses_multiline_meaningful_whitespace(tmp_path: Path, literal: str) -> None:
    path = tmp_path / "sample.js"
    original = f"const example = {literal};\n"
    path.write_bytes(original.encode())
    result = edit_file(tmp_path, path.name, [EditBlock(OLD, NEW)])
    assert not result.ok
    assert path.read_bytes() == original.encode()


def test_batch_with_alignment_validates_final_draft_before_writing(tmp_path: Path) -> None:
    path = tmp_path / "sample.ts"
    path.write_bytes(SOURCE.encode())
    result = edit_file(tmp_path, path.name, [EditBlock(OLD, NEW), EditBlock("function positive(value) {", "function ( {")])
    assert not result.ok and "invalid" in result.model_text
    assert path.read_bytes() == SOURCE.encode()
