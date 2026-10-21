"""Real entry/HTTP probe for admit_hints revocation between generations."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
from contextlib import closing
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from experiment_paths import DATA_ROOT, REPO

sys.path.insert(0, str(REPO))

from codey.agents.handoff import ConversationContext  # noqa: E402
from codey.app.context import AppContext  # noqa: E402
from codey.operations.context import RunFrame, RunWork  # noqa: E402
from codey.operations.task_entry import run_entry_kernel  # noqa: E402
from codey.providers.api_provider import ApiProvider  # noqa: E402
from codey.runtime.observe.execution_evidence import ExecutionEvidence  # noqa: E402
from codey.task.model import TaskSubmission  # noqa: E402
from codey.workspace.changes import collect_changes  # noqa: E402

HINT = "Synthetic revocable preference."
MODES = ("enabled", "disabled_initially", "disabled_after_first")


def probe(directory: Path, *, native: bool, mode: str) -> dict:
    directory.mkdir(parents=True, exist_ok=False)
    project = directory / "project"
    project.mkdir()
    (project / "fixture.txt").write_text("synthetic file", encoding="utf-8")
    payloads, transitions, errors = [], [], []
    session_id = "revocation-probe"
    with closing(AppContext(directory / "state")) as state:
        reader = state.ghost_inbox.control_enabled
        if mode == "disabled_initially" and not state.ghost_inbox.set_control(
            "admit_hints", False, scope="session", session_id=session_id,
        ):
            raise RuntimeError("initial scoped disable failed")

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                payloads.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                first = len(payloads) == 1
                if first and mode == "disabled_after_first":
                    ok = state.ghost_inbox.set_control("admit_hints", False, scope="session", session_id=session_id)
                    effective = reader(
                        "admit_hints", project=str(project), session_id=session_id,
                    )
                    transitions.append({"after_request": 1, "write_ok": ok, "effective_enabled": effective})
                    if not ok or effective:
                        errors.append("scoped revocation failed")
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
        provider = ApiProvider(f"http://127.0.0.1:{server.server_port}/v1", "synthetic-control-revocation",
                               native_tools=native, timeout=5, output_tokens=128)
        request = TaskSubmission(session_id, str(project), "Read fixture.txt and say done.", 2, False, "local",
                                 intent="chat", run_id="revocation-run")
        frame = RunFrame(request, request.run_id, "chat", provider, "local", str(project),
                         ConversationContext(), True, "", "", None, "", False, set(), 0)
        events = []
        reads = []

        def read_control(name, **scope):
            value = reader(name, **scope)
            if name == "admit_hints":
                reads.append({"request_count": len(payloads), "enabled": value,
                              "project_matches": scope.get("project") == str(project),
                              "session_matches": scope.get("session_id") == session_id})
            return value

        try:
            with patch("codey.operations.ghost_context.ghost_directive", return_value=SimpleNamespace(text=HINT)), \
                 patch.object(state.ghost_inbox, "control_enabled", read_control):
                outcome = run_entry_kernel(
                    frame, RunWork([], ExecutionEvidence()),
                    SimpleNamespace(on_event=events.append, on_shell_request=events.append),
                    SimpleNamespace(state=state, workspace_revisions=state.workspace_revisions,
                                    collect_changes=collect_changes),
                )
            result = outcome.event
            (directory / "result.json").write_text(json.dumps(result, default=str, indent=2), encoding="utf-8")
            if errors:
                raise RuntimeError(errors[0])
            initial_has_hint = bool(payloads) and HINT in json.dumps(payloads[0])
            if initial_has_hint != (mode != "disabled_initially"):
                raise RuntimeError("first-send on/off control did not match scoped authority")
            explicit_rejection = (mode == "disabled_after_first" and len(payloads) == 1
                                  and result["stop_reason"] == "provider_failure"
                                  and "hints_disabled" in result.get("summary", ""))
            if not explicit_rejection and (result["stop_reason"] != "done" or len(payloads) != 2):
                raise RuntimeError(f"probe incomplete: {result['stop_reason']}/{len(payloads)}")
            second_has_hint = len(payloads) == 2 and HINT in json.dumps(payloads[1])
            if mode == "enabled" and not second_has_hint:
                raise RuntimeError("enabled history control lost hint")
            return {"native": native, "mode": mode, "requests": len(payloads),
                    "stop_reason": result["stop_reason"], "first_payload_has_hint": initial_has_hint,
                    "second_payload_has_hint": second_has_hint, "explicit_revocation_rejection": explicit_rejection,
                    "revoked_hint_resent": mode == "disabled_after_first" and second_has_hint,
                    "control_transitions": transitions, "admit_hints_reads": reads}
        finally:
            (directory / "requests.json").write_text(json.dumps(payloads, indent=2), encoding="utf-8")
            (directory / "events.txt").write_text(repr(events), encoding="utf-8")
            provider.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", required=True)
    parser.add_argument("--metrics-out", type=Path)
    args = parser.parse_args()
    if Path(args.suite).name != args.suite or args.suite in {".", ".."}:
        parser.error("suite must be a directory name")
    root = DATA_ROOT / args.suite
    root.mkdir(parents=True, exist_ok=False)
    manifest = {
        "suite": args.suite, "model": "synthetic-control-revocation", "endpoint": "ephemeral loopback HTTP",
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
        "command": subprocess.list2cmdline(sys.argv), "started_at": datetime.now(UTC).isoformat(),
        "task": "read_fixture_then_done", "cells": 6, "reps": 1, "protocols": ["text", "native"],
        "controls": list(MODES), "context": 32768, "reserve": 8192, "keep_recent": 12000,
        "output_tokens": 128, "temperature": 0.3, "timeout_seconds": 5, "max_turns": 2,
        "candidate": "fixed synthetic directive fixture; real scoped GhostInboxStore authority",
        "hypothesis": "session disable after first generation does not prevent historical hint resends",
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    rows = []
    for native in (False, True):
        for mode in MODES:
            cell = f"{'native' if native else 'text'}-{mode}"
            try:
                row = probe(root / cell, native=native, mode=mode)
            except Exception as exc:
                (root / "failure.json").write_text(json.dumps({"cell": cell, "error": str(exc),
                    "at": datetime.now(UTC).isoformat()}), encoding="utf-8")
                raise
            rows.append(row)
            print(json.dumps(row), flush=True)
    metrics = {"schema": "ghost_control_revocation_probe_v1", "config": {
        key: value for key, value in manifest.items() if key not in {"command", "started_at"}},
        "rows": rows, "decision": "EXPERIMENT_ONLY", "model_behavior_score": None}
    (root / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    if args.metrics_out:
        args.metrics_out.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
