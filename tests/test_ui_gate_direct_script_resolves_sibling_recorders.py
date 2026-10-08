"""Direct UI-gate launches can resolve the production recorder sibling module."""
import subprocess
import sys
from pathlib import Path


def test_ui_gate_direct_script_imports_recorder_without_pythonpath(tmp_path):
    script = Path(__file__).resolve().parents[1] / "tools" / "local_model_ui_gate.py"
    probe = (
        "import pathlib, runpy, sys; "
        "gate = runpy.run_path(sys.argv[1], run_name='direct_ui_gate_probe'); "
        "scope = gate['_provider_history_recorder']('zen', pathlib.Path(sys.argv[2])); "
        "scope.__enter__(); scope.__exit__(None, None, None)"
    )
    result = subprocess.run([sys.executable, "-I", "-c", probe, str(script), str(tmp_path / "history.jsonl")],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf8", timeout=30)
    assert result.returncode == 0, result.stderr
