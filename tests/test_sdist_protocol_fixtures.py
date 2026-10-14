from __future__ import annotations

import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    "approval.json",
    "changes.json",
    "research.json",
    "run_state.json",
    "shell_result.json",
    "task_done.json",
}


def test_sdist_contains_shared_protocol_fixtures(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--no-isolation", "--outdir", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    archives = list(tmp_path.glob("*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(archives[0]) as archive:
        included = {
            Path(member.name).name
            for member in archive.getmembers()
            if member.isfile() and "/tests/fixtures/protocol/" in member.name
        }
    assert included >= REQUIRED
