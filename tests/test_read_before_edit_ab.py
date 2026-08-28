from __future__ import annotations

from pathlib import Path

from tests.manual import read_before_edit_ab as harness


def test_write_report_output_creates_parent_directory(tmp_path: Path) -> None:
    output = tmp_path / "nested" / "result.json"

    harness.write_report_output(output, "{}")

    assert output.read_text(encoding="utf-8") == "{}\n"
