"""Isolating application state must preserve installed Bun without a Node fallback."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROBE = """
import json, os, runpy, shutil
from pathlib import Path
runpy.run_path('tests/conftest.py')
first_path = os.environ['PATH']
runpy.run_path('tests/conftest.py')
from codey.providers.api_transport import GenerationNotSentError
from codey.providers.zen.node_http import fetch_runtime
try:
    runtime = fetch_runtime()
except GenerationNotSentError:
    runtime = None
print(json.dumps({'home': str(Path.home()), 'runtime': runtime,
                  'path_stable': first_path == os.environ['PATH']}))
"""


@pytest.mark.parametrize("installation", ["home", "path", "missing"])
def test_isolated_home_preserves_runtime_discovery_and_path_precedence(tmp_path: Path, installation: str) -> None:
    original_home = tmp_path / "original"
    isolated_home = tmp_path / "isolated"
    path_bin = tmp_path / "path-bin"
    path_bin.mkdir()
    name = "bun.exe" if os.name == "nt" else "bun"
    expected = None
    if installation != "missing":
        home_runtime = original_home / ".bun" / "bin" / name
        home_runtime.parent.mkdir(parents=True)
        home_runtime.write_bytes(b"")
        home_runtime.chmod(0o755)
        expected = home_runtime
    if installation == "path":
        expected = path_bin / name
        expected.write_bytes(b"")
        expected.chmod(0o755)
    env = {**os.environ, "HOME": str(original_home), "USERPROFILE": str(original_home),
           "PYTEST_STATE_HOME": str(isolated_home), "PATH": str(path_bin)}
    child = subprocess.run(
        [sys.executable, "-B", "-c", PROBE], env=env, cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, check=True, timeout=20,
    )
    result = json.loads(child.stdout)
    assert Path(result["home"]).resolve() == isolated_home.resolve()
    assert result["path_stable"]
    if expected is None:
        assert result["runtime"] is None
    else:
        assert Path(result["runtime"]).resolve() == expected.resolve()
