"""A fresh process without an importable Zen package still runs Local APIs."""
import json
import subprocess
import sys
from pathlib import Path


def test_local_protocols_bootstrap_history_and_gate_work_without_zen(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "tests.support.optional_zen_removal_probe", str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report.get("usage_without_zen") is True
    assert report == {"local_protocols": ["openai-completions", "openai-responses"],
                      "bootstrap": True, "cli": True, "old_history": True,
                      "old_connection_rejected": True, "local_gate": True, "zen_imports": [], "usage_without_zen": True}
