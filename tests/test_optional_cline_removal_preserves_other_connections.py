"""Removing Cline leaves Local, Zen, tool approval, history and startup intact."""
import json
import subprocess
import sys
from pathlib import Path


def test_cline_absence_in_a_fresh_process_preserves_the_runtime(tmp_path):
    result = subprocess.run([sys.executable, "-m", "tests.support.optional_cline_removal_probe", str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report == {"local_protocols": ["openai-completions", "openai-responses"],
        "bootstrap": True, "cli": True, "old_history": True, "old_connection_rejected": True, "local_gate": True,
        "cline_imports": [], "usage_without_cline": True, "workspace_shell_without_cline": True,
        "zen_without_cline": True, "auxiliary_without_cline": True,
        "child_cli_without_cline": True, "headless_without_cline": True, "review_without_cline": True}


def test_shared_production_sources_have_no_hidden_cline_dependency():
    root = Path(__file__).resolve().parents[1] / "codey"
    allowed = root / "providers" / "catalog.py"
    for path in root.rglob("*.py"):
        if "cline" in path.parts or path == allowed:
            continue
        text = path.read_text(encoding="utf8")
        assert "codey.providers.cline" not in text, path
        assert "api.cline.bot" not in text and "workos:" not in text, path
