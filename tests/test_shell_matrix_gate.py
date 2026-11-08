"""The CI entry point must reach real shell execution and explain failures."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def run_gate(tmp_path, *required):
    report = tmp_path / "artifacts" / "shell-matrix.json"
    result = subprocess.run(
        [sys.executable, "-m", "tools.shell_matrix_gate", "--required", *required, "--output", str(report)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=60,
    )
    return result, json.loads(report.read_text(encoding="utf-8"))


def test_gate_cli_executes_the_native_shell(tmp_path):
    name = "cmd" if os.name == "nt" else "sh"
    result, report = run_gate(tmp_path, name)
    assert result.returncode == 0, (result.stdout, result.stderr, report)
    modes = ["noninteractive"] if name == "cmd" else ["noninteractive", "login"]
    assert json.loads(result.stdout) == {"passed": len(modes), "cases": len(modes)}
    assert [row["startup"] for row in report["results"]] == modes
    for row in report["results"]:
        assert row["ok"] is True
        assert row["result"]["status"] == "exit"
        assert row["result"]["exit_code"] == (0 if name == "cmd" else 7)
        assert "SHELL_MATRIX" in row["result"]["output"]


def test_gate_cli_fails_on_missing_required_shell_and_prints_each_failure(tmp_path):
    name = "cmd" if os.name == "nt" else "sh"
    result, report = run_gate(tmp_path, "missing-shell-matrix-interpreter", name)
    assert result.returncode == 1
    failed = [row for row in report["results"] if not row["ok"]]
    assert len(failed) == 2
    assert all("ValueError" in row["error"] for row in failed)
    assert [json.loads(line)["failure"] for line in result.stderr.splitlines()] == failed
    passed = 1 if name == "cmd" else 2
    assert json.loads(result.stdout) == {"passed": passed, "cases": passed + 2}
