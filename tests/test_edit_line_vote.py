from __future__ import annotations

from pathlib import Path

from codey.toolchain.runtime import EditBlock, _line_vote_positions, edit_file


def test_line_vote_counts_repeated_offsets_and_accepts_a_partial_tail() -> None:
    assert _line_vote_positions(
        ["{", "prepare()", "return None", "fallback()", "return None"],
        ["prepare()", "return None", "misspelled()", "return None", "extra()"],
    ) == (1,)
    assert _line_vote_positions(["{", "}", "}", ")"], ["{", "}", ")"]) == ()


def test_failed_multiline_edit_displays_the_matching_line_offsets(tmp_path: Path) -> None:
    path = tmp_path / "converter.py"
    original = (b"def original():\r\n    init()\r\n    old_step()\r\n    compute()\r\n"
                b"\r\ndef other():\r\n    compute()\r\n    old_step()\r\n    init()\r\n")
    path.write_bytes(original)
    corrected = "    init()\n    new_step()\n    compute()"
    rejected = edit_file(tmp_path, path.name, [EditBlock("    init()\n    old_stpe()\n    compute()", corrected)])
    assert not rejected.ok
    assert "Candidate replacement starts at lines: 2." in rejected.model_text
    assert "3 |     old_step()" in rejected.model_text and path.read_bytes() == original
    applied = edit_file(tmp_path, path.name, [EditBlock("    init()\n    old_step()\n    compute()", corrected)])
    assert applied.ok
    assert path.read_bytes() == original.replace(b"old_step()", b"new_step()", 1)
