"""Human CLI progress identifies the same tool as the machine event."""

from codey.app.cli import _human_cli_line


def test_cli_progress_preserves_canonical_tool_identity():
    row = {"type": "tool", "turn": 2, "tool": "read", "tool_name": "read_file", "path": "app.py", "ok": False}
    assert _human_cli_line(row) == "[turn 2] read_file app.py failed"


def test_cli_progress_never_calls_a_truthy_non_boolean_success():
    assert _human_cli_line({"type": "tool", "tool_name": "run", "ok": "false"}).endswith("failed")
