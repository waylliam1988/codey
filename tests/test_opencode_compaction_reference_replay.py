"""Reference replay executes OpenCode source and fails explicitly on source drift."""
import shutil
from pathlib import Path

import pytest

from tools.context_compaction_benchmark.opencode import select_opencode


def test_actual_opencode_functions_preserve_recent_turn_and_use_their_summary_template():
    if shutil.which("node") is None:
        pytest.skip("Node.js unavailable")
    root = Path(__file__).resolve().parent / "fixtures" / "opencode_compaction_reference"
    entries = [{"seq": 1, "message": {"type": "user", "text": "old constraint " * 1000}},
               {"seq": 2, "message": {"type": "user", "text": "Correction: edit app.py only"}}]
    result = select_opencode(root, entries, keep_tokens=2000)
    assert "old constraint" in result["head"]
    assert "Correction: edit app.py only" in result["recent"]
    assert "## Objective" in result["prompt"] and "## Next Move" in result["prompt"]


def test_actual_opencode_update_prompt_prioritizes_the_new_conversation():
    if shutil.which("node") is None:
        pytest.skip("Node.js unavailable")
    root = Path(__file__).resolve().parent / "fixtures" / "opencode_compaction_reference"
    entries = [{"seq": 1, "message": {"type": "user", "text": "new corrected target" * 1000}}]
    result = select_opencode(root, entries, keep_tokens=1, previous_summary="Keep no-db")
    assert "Keep no-db" in result["prompt"]
    assert "conversation wins" in result["prompt"]
