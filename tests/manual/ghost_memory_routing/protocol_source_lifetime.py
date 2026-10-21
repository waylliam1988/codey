"""Measure Ghost ownership across real task/window/durable lifetimes (offline)."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from experiment_paths import DATA_ROOT, REPO

sys.path.insert(0, str(REPO))

from codey.app.context import AppContext  # noqa: E402
from codey.app.task_services import build_task_deps  # noqa: E402
from codey.operations.conversation_plan import plan_conversation  # noqa: E402
from codey.operations.task_entry import run_task_submission  # noqa: E402
from codey.providers.api_provider import ApiProvider  # noqa: E402
from codey.task.model import StartTask, TaskSubmission  # noqa: E402

HINT = "Synthetic source lifetime preference ZXQ471."
MODEL = "synthetic-source-lifetime"
PROVIDER = "ghost-fixture"
SESSION = "source-lifetime"
LIFETIMES = ("warm", "window_reopen", "fresh_provider", "app_restart")
PROTOCOLS = ("openai-completions", "openai-responses")


def encode(value):
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def contains(value):
    return HINT in encode(value)


def fingerprint(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def probe(directory: Path, *, protocol: str, lifetime: str, revoke: bool) -> dict:
    directory.mkdir(parents=True, exist_ok=False)
    project = directory / "project"
    project.mkdir()
    state_home = directory / "state"
    payloads, events, plans, checkpoints = [], [], [], []
    providers = []
    state = AppContext(state_home)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payloads.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            call_id = f"done-{len(payloads)}"
            # Deliberately non-echoing reply: hint retention cannot come from the model.
            arguments = '{"summary":"Synthetic task completed."}'
            if protocol == "openai-completions":
                reply = {"choices": [{"message": {"role": "assistant", "content": None,
                    "tool_calls": [{"id": call_id, "type": "function", "function": {
                        "name": "done", "arguments": arguments}}]}, "finish_reason": "stop"}]}
            else:
                reply = {"status": "completed", "output": [{"type": "function_call", "call_id": call_id,
                    "name": "done", "arguments": arguments}]}
            body = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}/v1"

    def new_provider():
        client = ApiProvider(endpoint, MODEL, api_protocol=protocol, native_tools=True,
                             timeout=5, temperature=0, output_tokens=128, context_window_tokens=65536)
        providers.append(client)
        return client

    provider = new_provider()

    def measured_plan(**kwargs):
        plan = plan_conversation(**kwargs)
        context = getattr(plan.window_action, "context", None)
        plans.append({"fresh_chat": plan.fresh_chat, "action": type(plan.window_action).__name__,
                      "handoff_has_hint": HINT in plan.handoff,
                      "replay_has_hint": context is not None and contains(list(context.messages)),
                      "replay_messages": len(context.messages) if context is not None else None})
        return plan

    def snapshot(label):
        durable = state.durable.context_snapshot(SESSION, token_budget=60000)
        entries = state.durable.entries(SESSION)
        row = {"label": label, "transport_has_hint": contains(provider._messages),
               "export_has_hint": HINT in provider.export_work_state(),
               "durable_context_has_hint": contains(list(durable.messages)),
               "durable_entries_has_hint": any(HINT in str(entry) for entry in entries),
               "conversation_snapshot_has_hint": contains(state.conversation_for(SESSION).snapshot.to_payload()),
               "durable_messages": len(durable.messages), "durable_entries": len(entries),
               "durable_digest": durable.digest, "transport_digest": fingerprint(provider._messages)}
        checkpoints.append(row)

    def submit(index):
        state.emit = events.append
        request = TaskSubmission.from_input(
            session_id=SESSION, project=str(project), max_turns=1, provider_id=PROVIDER,
            input=StartTask(f"Say task {index} is complete."), intent="chat", run_id=f"source-run-{index}")
        deps = build_task_deps(state, connect_provider=lambda _provider_id: provider)
        start = len(payloads)
        with patch("codey.operations.ghost_context.ghost_directive", return_value=SimpleNamespace(text=HINT)), \
             patch("codey.operations.task_phases.dispatch.plan_conversation", measured_plan):
            run_task_submission(deps, request)
        terminal = [event for event in events if event.get("type") == "task_done"
                    and event.get("run_id") == request.run_id]
        if len(terminal) != 1 or terminal[0].get("stop_reason") != "done" or len(payloads) != start + 1:
            raise RuntimeError(f"incomplete task {index}: terminal={terminal!r}, HTTP delta={len(payloads) - start}")
        effects = state.runtime_effects.load_effects(SESSION, request.run_id)
        sends = [effect for effect in effects if effect.intent.effect_category == "provider_send"]
        if (sorted(effect.intent.display_ref for effect in sends) != ["acknowledge_tool_results", "send_turn"]
                or any(effect.settlement is None or effect.settlement.status != "ok" for effect in sends)):
            raise RuntimeError(f"missing actual successful provider settlement: {sends!r}")
        snapshot(f"after_task_{index}")
        return {"http": len(payloads) - start, "stop_reason": terminal[0]["stop_reason"],
                "provider_settlements": len(sends), "settled_methods": sorted(effect.intent.display_ref for effect in sends),
                "hint_sent": contains(payloads[-1])}

    try:
        first = submit(1)
        if not first["hint_sent"]:
            raise RuntimeError("initial enabled control failed")
        if revoke and not state.ghost_inbox.set_control("admit_hints", False, scope="session", session_id=SESSION):
            raise RuntimeError("scoped revocation write failed")
        old_window = provider.window_ref
        if lifetime == "window_reopen":
            # Explicit production registry invalidation, not a mocked planner verdict.
            # This is a window reopen; it does not simulate token-pressure rollover.
            state.set_provider_session(PROVIDER, None)
        elif lifetime in {"fresh_provider", "app_restart"}:
            provider.close()
            provider = new_provider()
            if lifetime == "app_restart":
                if not state.close():
                    raise RuntimeError("AppContext did not close cleanly")
                state = AppContext(state_home)
        effective = state.ghost_inbox.control_enabled("admit_hints", project=str(project), session_id=SESSION)
        if effective != (not revoke):
            raise RuntimeError("scoped authority lost across lifecycle")
        snapshot("before_task_2")
        second = submit(2)
        if len(plans) != 2:
            raise RuntimeError("did not traverse real conversation planning twice")
        if lifetime == "warm" and (plans[1]["fresh_chat"] or provider.window_ref != old_window):
            raise RuntimeError("warm control unexpectedly opened a window")
        if lifetime != "warm" and (not plans[1]["fresh_chat"] or provider.window_ref == old_window):
            raise RuntimeError("lifecycle failed to open a distinct window")
        if not revoke and not second["hint_sent"]:
            raise RuntimeError("healthy on control lost current hint")
        return {"protocol": protocol, "lifetime": lifetime, "revoke": revoke,
                "first": first, "second": second, "plans": plans, "checkpoints": checkpoints,
                "revoked_hint_resent": revoke and second["hint_sent"],
                "window_changed": provider.window_ref != old_window}
    finally:
        (directory / "requests.json").write_text(encode(payloads), encoding="utf-8")
        (directory / "events.json").write_text(encode(events), encoding="utf-8")
        (directory / "plans.json").write_text(encode(plans), encoding="utf-8")
        (directory / "checkpoints.json").write_text(encode(checkpoints), encoding="utf-8")
        for client in providers:
            client.close()
        state.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", required=True)
    parser.add_argument("--metrics-out", type=Path)
    parser.add_argument("--lifetime", choices=LIFETIMES, nargs="+", default=LIFETIMES)
    parser.add_argument("--protocol", choices=PROTOCOLS, nargs="+", default=PROTOCOLS)
    args = parser.parse_args()
    if Path(args.suite).name != args.suite or args.suite in {".", ".."}:
        parser.error("suite must be a directory name")
    root = DATA_ROOT / args.suite
    root.mkdir(parents=True, exist_ok=False)
    manifest = {"suite": args.suite, "model": MODEL, "endpoint": "ephemeral loopback HTTP",
                "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
                "command": subprocess.list2cmdline(sys.argv), "started_at": datetime.now(UTC).isoformat(),
                "protocols": list(args.protocol), "lifetimes": list(args.lifetime),
                "cells": len(args.protocol) * len(args.lifetime) * 2, "tasks_per_cell": 2, "reps": 1,
                "on_off": "enabled first task; enabled versus actual scoped disable before second",
                "context": 65536, "reserve": 8192, "keep_recent": 12000, "output_tokens": 128,
                "temperature": 0, "timeout_seconds": 5, "max_turns": 1,
                "hypothesis": "warm history and exported handoff retain hints absent from durable replay",
                "fixture": "fixed directive producer and non-echoing native done response; production gates unchanged"}
    (root / "manifest.json").write_text(encode(manifest), encoding="utf-8")
    rows = []
    for protocol in args.protocol:
        for lifetime in args.lifetime:
            for revoke in (False, True):
                cell = f"{protocol}-{lifetime}-{'revoke' if revoke else 'on'}"
                try:
                    row = probe(root / cell, protocol=protocol, lifetime=lifetime, revoke=revoke)
                except Exception as exc:
                    (root / "failure.json").write_text(encode({"cell": cell, "error": repr(exc),
                        "at": datetime.now(UTC).isoformat()}), encoding="utf-8")
                    raise
                rows.append(row)
                print(encode(row), flush=True)
    metrics = {"schema": "ghost_protocol_source_lifetime_v1", "config": {
        key: value for key, value in manifest.items() if key not in {"command", "started_at"}},
        "rows": rows, "decision": "EXPERIMENT_ONLY", "model_behavior_score": None}
    (root / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    if args.metrics_out:
        args.metrics_out.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
