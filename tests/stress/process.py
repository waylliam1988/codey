"""process.py: parent-side helpers for real OS-kill tests (Durability Lab Level 3).

Owns spawning the ``kill_worker`` child, waiting for its rendezvous,
killing it with ``Popen.kill()`` (TerminateProcess on Windows, SIGKILL on
POSIX), and respawning it for canonical recovery. Both the idle-checkpoint
suite and the crash-point matrix build on these; neither duplicates them.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from tests.stress.model import normalize

ROOT = Path(__file__).resolve().parents[2]
WORKER = Path(__file__).resolve().parent / "kill_worker.py"
PHASE_TIMEOUT = 60.0


def _child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _spawn(mode: str, state_home: Path, steps: list | None = None):
    cmd = [sys.executable, "-u", str(WORKER), "--mode", mode, "--state-home", str(state_home)]
    if steps is not None:
        cmd += ["--steps", json.dumps(steps)]
    return subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_child_env(),
    )


def _spawn_crashpoint(point: str, state_home: Path, rendezvous: Path):
    return subprocess.Popen(
        [
            sys.executable, "-u", str(WORKER), "--mode", "crashpoint",
            "--state-home", str(state_home), "--point", point,
            "--rendezvous", str(rendezvous),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_child_env(),
    )


def _wait_file(path: Path, timeout: float = PHASE_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"checkpoint never appeared: {path}")
        time.sleep(0.05)


def _read_line(proc, timeout: float = PHASE_TIMEOUT) -> dict:
    line = proc.stdout.readline()
    if not line:
        raise TimeoutError("child produced no output")
    return json.loads(line)


def _kill(proc) -> None:
    proc.kill()
    code = proc.wait(timeout=PHASE_TIMEOUT)
    proc.stderr.read()
    if code == 0:
        raise AssertionError("child exited cleanly, expected a kill")


def _recover_canonical(state_home: Path) -> dict:
    proc = _spawn("recover", state_home)
    try:
        out, _ = proc.communicate(timeout=PHASE_TIMEOUT)
        if proc.returncode != 0:
            raise AssertionError(f"recover child failed: {proc.returncode}")
    finally:
        if proc.poll() is None:
            proc.kill()
    payload = json.loads(out.strip().splitlines()[-1])
    return normalize(payload["canonical"])


__all__ = [
    "PHASE_TIMEOUT",
    "WORKER",
    "_child_env",
    "_kill",
    "_read_line",
    "_recover_canonical",
    "_spawn",
    "_spawn_crashpoint",
    "_wait_file",
]
