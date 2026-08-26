"""Local CI: the single release gate for Codey.

Runs every check the project treats as authoritative before a commit or a
release: ruff, the full pytest suite, JavaScript asset syntax checks, and
the completion-enforcement A/B self-test. Designed for local execution and
the checked-in pre-commit hook; GitHub CI stays available only as a manual
trigger.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(label: str, command: list[str]) -> bool:
    print(f"==> {label}", flush=True)
    try:
        result = subprocess.run(command, cwd=str(ROOT), check=False)
    except OSError as exc:
        print(f"==> FAILED {label}: {exc}", flush=True)
        return False
    if result.returncode != 0:
        print(f"==> FAILED {label} (exit {result.returncode})", flush=True)
        return False
    return True


def _check_js_assets() -> bool:
    assets = sorted((ROOT / "codey" / "web" / "assets").glob("*.js"))
    if shutil.which("node") is None:
        print("==> js-syntax SKIPPED: node is not installed on this machine", flush=True)
        return True
    if not assets:
        print("==> js-syntax: no JS assets found", flush=True)
        return True
    for asset in assets:
        if not _run(f"js-syntax: node --check {asset.name}", ["node", "--check", str(asset)]):
            return False
    return True


def main() -> int:
    steps: list[tuple[str, list[str]]] = [
        ("ruff", [sys.executable, "-m", "ruff", "check", "."]),
        ("pytest", [sys.executable, "-m", "pytest", "-q"]),
        ("ab-self-test", [
            sys.executable,
            "-B",
            str(ROOT / "tests" / "manual" / "completion_enforcement_ab.py"),
            "--self-test",
        ]),
    ]
    for label, command in steps:
        if not _run(label, command):
            return 1
    if not _check_js_assets():
        return 1
    print("==> local CI passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
