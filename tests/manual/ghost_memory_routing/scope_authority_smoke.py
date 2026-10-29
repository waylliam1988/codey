"""Isolated production planning builder/kernel/API smoke; no behavior promotion."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from experiment_paths import DATA_ROOT, REPO

sys.path.insert(0, str(REPO))

from codey.agents.request import AgentRequest  # noqa: E402
from codey.policies.task_policy import TaskPolicy  # noqa: E402
from codey.providers.api_provider import ApiProvider  # noqa: E402
from codey.providers.api_request_policy import LocalRequestPolicy  # noqa: E402
from tests.support.kernel_runtime import run_agent as run  # noqa: E402

MODEL = "koboldcpp/Gemma4-12B-QAT-Uncensored-HauhauCS-Balanced-Q4_K_M"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["baseline", "candidate"], required=True)
    parser.add_argument("--attempt", default="", help="New implementation attempt; never overwrites a prior phase")
    parser.add_argument("--suite", default="scope_authority_20261021")
    parser.add_argument("--endpoint", default="http://127.0.0.1:5001/v1")
    parser.add_argument("--model", default=MODEL)
    args = parser.parse_args()
    if Path(args.suite).name != args.suite or args.suite in {".", ".."}:
        parser.error("suite must be a directory name")
    if args.attempt and (Path(args.attempt).name != args.attempt or args.attempt in {".", ".."}):
        parser.error("attempt must be a directory name")
    root = DATA_ROOT / args.suite
    project = root / "project"
    project.mkdir(parents=True, exist_ok=True)
    phase = root / (f"{args.phase}-{args.attempt}" if args.attempt else args.phase)
    phase.mkdir(exist_ok=False)
    config = {
        "suite": args.suite, "phase": args.phase, "attempt": args.attempt,
        "endpoint": args.endpoint, "model": args.model,
        "started_at": datetime.now(UTC).isoformat(),
        "command": subprocess.list2cmdline(sys.argv),
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
        "context": 262144, "reserve": 32768, "keep_recent": 32000,
        "output_tokens": 256, "temperature": 0, "thinking_enabled": False,
        "native_tools": True, "timeout_seconds": 180, "max_attempts": 1,
        "tasks": 1, "cells": ["on", "off"], "reps": 1,
        "production_entry": "project_adapter.run", "profile": "planning_readonly",
        "policy_grants": ["control"], "max_turns": 1,
        "note": "Structural smoke only; no frozen language evaluation or behavioral score.",
        "implementation_sha256": {
            name: hashlib.sha256((REPO / name).read_bytes()).hexdigest()
            for name in (
                "codey/operations/task_loop.py", "codey/operations/kernel_trace.py",
                "codey/operations/project_adapter.py", "codey/workspace/context_admission.py",
            )
        },
    }
    write_json(phase / "manifest.json", config)
    cell = "health"
    try:
        with urllib.request.urlopen(args.endpoint.rstrip("/") + "/models", timeout=10) as response:
            health = json.load(response)
        write_json(phase / "health.json", health)
        if args.model not in {row["id"] for row in health["data"]}:
            raise RuntimeError("requested model not loaded")
        for cell in config["cells"]:
            cell_root = phase / cell
            cell_root.mkdir()
            payloads, events, usages = [], [], []

            def capture(payload, payloads=payloads, cell_root=cell_root):
                payloads.append(json.loads(json.dumps(payload)))
                write_json(cell_root / "requests.json", payloads)

            policy = LocalRequestPolicy()
            policy.max_attempts = 1
            provider = ApiProvider(
                args.endpoint, args.model, timeout=180, temperature=0, thinking_enabled=False,
                context_window_tokens=262144, context_reserve_tokens=32768,
                context_keep_recent_tokens=32000, output_tokens=256, native_tools=True,
                configure_request=capture, request_policy=policy,
            )
            provider.on_usage = lambda usage, usages=usages: usages.append(asdict(usage))
            started = time.perf_counter()
            try:
                result = run(AgentRequest(
                    provider=provider, project=project, task="Reply with a short greeting.",
                    max_turns=1, permission_profile="planning_readonly",
                    task_policy=TaskPolicy(grants=frozenset({"control"})), provider_id="local",
                    ghost_directive="Prefer Chinese answers.", admit_hints=cell == "on",
                    coding_context_enabled=False, session_id="scope-smoke", run_id=f"scope-smoke-{cell}",
                    on_event=lambda event, events=events: events.append(event),
                ))
                write_json(cell_root / "result.json", asdict(result))
                write_json(cell_root / "events.json", events)
                write_json(cell_root / "usage.json", usages)
                if result.stop_reason != "done":
                    raise RuntimeError(f"non-done runtime result: {result.stop_reason}")
                if len(payloads) != 1:
                    raise RuntimeError("expected exactly one generation")
                prompt = payloads[0]["messages"][-1]["content"]
                if ("Prefer Chinese answers." in prompt) != (cell == "on"):
                    raise RuntimeError("hint wire placement mismatch")
                row = {"cell": cell, "stop_reason": result.stop_reason, "requests": len(payloads),
                       "seconds": round(time.perf_counter() - started, 3), "prompt_chars": len(prompt),
                       "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                       "request_sha256": hashlib.sha256(json.dumps(payloads[0], sort_keys=True).encode()).hexdigest(),
                       "hint_present": cell == "on"}
                write_json(cell_root / "metrics.json", row)
                print(json.dumps(row), flush=True)
            finally:
                provider.close()
    except Exception as exc:
        failure = {"cell": cell, "at": datetime.now(UTC).isoformat(), "type": type(exc).__name__,
                   "error": str(exc), "model": args.model, "endpoint": args.endpoint,
                   "command": config["command"], "retry": False}
        write_json(phase / "failure.json", failure)
        print(json.dumps({"status": "STOPPED", "cell": cell, "type": type(exc).__name__,
                          "archive": str(phase)}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
