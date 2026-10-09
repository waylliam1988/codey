# ruff: noqa: E402 - direct script execution adds the repository root first.

"""Compare Codey before and after the coding/research kernel unification.

This is a manual local-model experiment. Both arms use the same fixture,
provider endpoint, task and output budget. Sampling uses each version's provider
defaults, recorded in the observed requests. Native tools are off by
default so the comparison exercises the shared text protocol instead of
mixing the kernel change with the newer native-provider path.

Example (PowerShell)::

    python tests/manual/kernel_unification_ab.py --run-dir E:\\codey\\tests\\manual\\results\\kernel-unification-ab-12b-r1
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.manual.agent_stability_cases import TASK
from tests.manual.agent_stability_measurements import usage_totals
from tests.manual.codey_vs_pi_agent_stability_ab import (
    MODEL_ID,
    _event_rows,
    _fixture,
    _json_line,
    _metrics,
    _new_project_root,
    _Proxy,
    _run_verification,
)

LEGACY_COMMIT = "958bcb485bf05d0ae8232763681d1df5ecee1d34"
DEFAULT_LEGACY_ROOT = Path(__file__).resolve().parents[2] / "reference-projects" / (
    "codey-pre-unified-958bcb4"
)


def _safe_extract_tar(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as stream:
        members = stream.getmembers()
        root = destination.resolve()
        for member in members:
            target = (destination / member.name).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"git archive escaped destination: {member.name}")
        stream.extractall(destination, filter="data")


def materialize_commit(repo_root: Path, commit: str, destination: Path) -> Path:
    """Materialize one immutable git commit outside the active checkout."""
    if destination.exists():
        return destination.resolve()
    archive = Path(tempfile.mkstemp(prefix="codey-unification-", suffix=".tar")[1])
    try:
        with archive.open("wb") as handle:
            subprocess.run(
                ["git", "archive", "--format=tar", commit],
                cwd=repo_root,
                check=True,
                stdout=handle,
                stderr=subprocess.PIPE,
                text=False,
            )
        _safe_extract_tar(archive, destination)
    finally:
        archive.unlink(missing_ok=True)
    return destination.resolve()


def _source_root_for_arm(arm: str, repo_root: Path, legacy_root: Path) -> Path:
    if arm == "codey_unified":
        return repo_root.resolve()
    if arm == "codey_pre_unified":
        return legacy_root.resolve()
    raise ValueError(f"unknown arm: {arm}")


def _codey_command(project_root: Path, state_home: Path, max_turns: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "codey",
        "agent",
        "--json",
        "--provider",
        "local",
        "--project",
        str(project_root),
        "--state-home",
        str(state_home),
        "--max-turns",
        str(max_turns),
        TASK,
    ]


def _run_arm(
    arm: str,
    source_root: Path,
    project_root: Path,
    run_dir: Path,
    proxy_url: str,
    *,
    model_id: str,
    max_turns: int,
    max_tokens: int,
    native_tools: bool,
) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = run_dir / f"{arm}.stdout.log"
    stderr_path = run_dir / f"{arm}.stderr.log"
    events_path = run_dir / f"{arm}.events.jsonl"
    trace = run_dir / "trace"
    trace.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(source_root) + os.pathsep + env.get("PYTHONPATH", ""),
            "NATIVE_TOOLS": "1" if native_tools else "0",
            "LOCAL_OPENAI_BASE_URL": f"{proxy_url}/v1",
            "LOCAL_OPENAI_MODEL": model_id,
            "LOCAL_OPENAI_API_KEY": "local",
            "LOCAL_OPENAI_CONTEXT_WINDOW": "32768",
            "LOCAL_OPENAI_CONTEXT_RESERVE": str(max_tokens),
            "LOCAL_OPENAI_CONTEXT_KEEP": "12000",
            "CODEY_AB_TRACE": str(trace),
        }
    )
    command = _codey_command(project_root, run_dir / "state", max_turns)
    started = time.perf_counter()
    process = subprocess.run(
        command,
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=900,
        env=env,
        check=False,
    )
    wall_time = round(time.perf_counter() - started, 3)
    stdout_path.write_text(process.stdout, encoding="utf-8", errors="replace")
    stderr_path.write_text(process.stderr, encoding="utf-8", errors="replace")
    rows = _event_rows(process.stdout)
    events_path.write_text(
        "\n".join(_json_line(row) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    verification = _run_verification(project_root, trace=trace)
    return {
        "arm": arm,
        "status": "completed",
        "source_root": str(source_root),
        "wall_time_seconds": wall_time,
        "verification": verification,
        "metrics": _metrics(rows, [], verification, process.returncode),
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "events": str(events_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--legacy-root", type=Path, default=DEFAULT_LEGACY_ROOT)
    parser.add_argument("--commit", default=LEGACY_COMMIT)
    parser.add_argument("--upstream", default="http://127.0.0.1:5001")
    parser.add_argument("--proxy-port", type=int, default=5021)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--model", default=MODEL_ID)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--native-tools", action="store_true")
    args = parser.parse_args()

    repo_root = ROOT
    legacy_root = args.legacy_root.resolve()
    if not legacy_root.exists():
        materialize_commit(repo_root, args.commit, legacy_root)
    if not (legacy_root / "codey").is_dir():
        raise SystemExit(f"legacy source is missing codey/: {legacy_root}")

    run_dir = args.run_dir.resolve()
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    proxy = _Proxy(
        ("127.0.0.1", args.proxy_port),
        args.upstream,
        timeout=900.0,
    )
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    project_root = _new_project_root()
    result: dict[str, Any] = {
        "baseline_commit": args.commit,
        "model": args.model,
        "task": TASK,
        "sampling": "production provider defaults; see observed requests",
        "max_tokens": args.max_tokens,
        "native_tools": args.native_tools,
        "proxy": f"http://127.0.0.1:{args.proxy_port}",
        "arms": {},
    }
    try:
        for arm in ("codey_pre_unified", "codey_unified"):
            project = project_root / arm
            _fixture(project)
            proxy.active_arm = arm
            result["arms"][arm] = _run_arm(
                arm,
                _source_root_for_arm(arm, repo_root, legacy_root),
                project,
                run_dir / arm,
                result["proxy"],
                model_id=args.model,
                max_turns=args.max_turns,
                max_tokens=args.max_tokens,
                native_tools=args.native_tools,
            )
        records = proxy.records
        (run_dir / "proxy-records.json").write_text(_json_line(records), encoding="utf-8")
        for arm in result["arms"]:
            arm_records = [record for record in records if record.get("arm") == arm]
            result["arms"][arm]["request_records"] = len(arm_records)
            result["arms"][arm].update(usage_totals(arm_records))
        (run_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    finally:
        proxy.shutdown()
        proxy.server_close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all(item.get("status") == "completed" for item in result["arms"].values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
