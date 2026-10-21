"""Deterministic negative probe: expire a hint between real HTTP generations."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
from dataclasses import replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from experiment_paths import DATA_ROOT, REPO

sys.path.insert(0, str(REPO))

from codey.agents.request import AgentRequest  # noqa: E402
from codey.operations.project_adapter import _project_context, _project_ghost_sources  # noqa: E402
from codey.operations.task_loop import (  # noqa: E402
    KernelExecutionDeps,
    KernelRunRequest,
    KernelTransportDeps,
    run_task_kernel,
)
from codey.operations.task_session import TaskSession  # noqa: E402
from codey.policies.task_policy import TaskPolicy  # noqa: E402
from codey.providers.api_provider import ApiProvider  # noqa: E402
from codey.workspace.context_admission import request_scope  # noqa: E402

HINT = "Synthetic lifetime preference."


def probe(directory: Path, *, native: bool, expiring: bool) -> dict:
    payloads = []

    class Clock(datetime):
        current = datetime(2040, 1, 1, tzinfo=UTC)
        reads = []

        @classmethod
        def now(cls, tz=None):
            cls.reads.append(cls.current.isoformat())
            return cls.current

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payloads.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            Clock.current = datetime(2040, 1, 1, 0, 0, 2, tzinfo=UTC)
            first = len(payloads) == 1
            name, args = ("read_file", {"path": "fixture.txt"}) if first else ("done", {"summary": "done"})
            message = {"role": "assistant", "content": json.dumps({"tool": name, "args": args})}
            if native:
                message = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": f"call-{len(payloads)}", "type": "function", "function": {
                        "name": name, "arguments": json.dumps(args)}}]}
            body = json.dumps({"choices": [{"message": message, "finish_reason": "stop"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/v1"
    provider = ApiProvider(endpoint, "synthetic-history-expiry", native_tools=native, timeout=5, output_tokens=128)
    directory.mkdir(parents=True, exist_ok=False)
    request = AgentRequest(provider=provider, project=directory, task="Read fixture and say done.",
                           ghost_directive=HINT, permission_profile="planning_readonly", session_id="expiry-probe")
    sources = _project_ghost_sources(request)
    if expiring:
        sources = (replace(sources[0], expires_at="2040-01-01T00:00:01Z"),)
    try:
        with patch("codey.workspace.context_admission.datetime", Clock):
            result = run_task_kernel(
                TaskSession(policy=TaskPolicy(frozenset({"project.read", "control"})),
                            project=str(directory), max_turns=2, coding_context_enabled=False),
                request=KernelRunRequest(
                    transport=KernelTransportDeps(provider=provider, user_task=request.task,
                                                  context_text=_project_context(request), ghost_sources=sources,
                                                  context_scope=request_scope(directory, request.session_id)),
                    execution=KernelExecutionDeps(executors={"read_file": lambda call: "synthetic file"},
                                                  project_path=directory, session_id=request.session_id),
                ),
            )
        (directory / "requests.json").write_text(json.dumps(payloads, ensure_ascii=False, indent=2), encoding="utf-8")
        (directory / "result.txt").write_text(repr(result), encoding="utf-8")
        blocked = (expiring and result.stop_reason == "provider_failure" and len(payloads) == 1
                   and "Ghost context admission rejected" in result.summary and ":expired" in result.summary)
        if not blocked and (result.stop_reason != "done" or len(payloads) != 2):
            raise RuntimeError(f"probe did not complete or explicitly reject expiry: {result.stop_reason}/{len(payloads)}")
        if HINT not in json.dumps(payloads[0]["messages"]):
            raise RuntimeError("first-send control missing hint")
        second_has_hint = len(payloads) == 2 and HINT in json.dumps(payloads[1]["messages"])
        return {"native": native, "expiring": expiring, "requests": len(payloads),
                "stop_reason": result.stop_reason, "admission_clock_reads": Clock.reads,
                "expiry_explicitly_blocked": blocked,
                "second_payload_has_hint": second_has_hint,
                "expired_hint_sent": expiring and second_has_hint}
    finally:
        provider.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default="provider_history_expiry_20261021")
    parser.add_argument("--metrics-out", type=Path)
    args = parser.parse_args()
    if Path(args.suite).name != args.suite or args.suite in {".", ".."}:
        parser.error("suite must be a directory name")
    root = DATA_ROOT / args.suite
    root.mkdir(parents=True, exist_ok=False)
    manifest = {
        "suite": args.suite, "model": "synthetic-history-expiry", "endpoint": "ephemeral loopback HTTP",
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
        "started_at": datetime.now(UTC).isoformat(), "command": subprocess.list2cmdline(sys.argv),
        "tasks": ["read_fixture_then_done"], "cells": 4, "reps": 1, "protocols": ["text", "native"],
        "context": 32768, "reserve": 8192, "keep_recent": 12000, "output_tokens": 128,
        "temperature": 0.3, "timeout_seconds": 5, "max_turns": 2,
        "hypothesis": "expired hints remain in second actual HTTP request history",
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    rows = []
    for native in (False, True):
        for expiring in (False, True):
            name = f"{'native' if native else 'text'}-{'expiring' if expiring else 'control'}"
            try:
                row = probe(root / name, native=native, expiring=expiring)
            except Exception as exc:
                (root / "failure.json").write_text(json.dumps({"cell": name, "error": str(exc),
                                                                "at": datetime.now(UTC).isoformat()}), encoding="utf-8")
                raise
            rows.append(row)
            print(json.dumps(row), flush=True)
    (root / "metrics.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    if args.metrics_out:
        sanitized = {"schema": "ghost_history_expiry_probe_v1", "config": {
            key: value for key, value in manifest.items() if key not in {"command", "started_at"}},
            "rows": rows, "decision": "EXPERIMENT_ONLY", "model_behavior_score": None}
        args.metrics_out.write_text(json.dumps(sanitized, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
