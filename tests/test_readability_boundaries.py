"""Local readability budgets for the three reviewed orchestration functions."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {
    "codey/runtime/core/operation_reducer.py": "next_runtime_action",
    "codey/runs/receipt.py": "task_receipt_from_payload",
    "codey/operations/kernel_protocol.py": "normalize_turn",
}


@pytest.fixture(scope="module")
def complexity_diagnostics():
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", *TARGETS, "--isolated", "--ignore-noqa",
         "--select", "C901", "--config", "lint.mccabe.max-complexity=10", "--output-format", "json"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode in (0, 1), result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("path,name", TARGETS.items())
def test_reviewed_orchestration_function_has_mccabe_complexity_at_most_ten(path, name, complexity_diagnostics):
    matching = [row["message"] for row in complexity_diagnostics
                if Path(row["filename"]) == ROOT / path and row["message"].startswith(f"`{name}` ")]
    assert not matching, matching
