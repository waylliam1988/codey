"""EXPERIMENT_ONLY: move the existing validator across real API send stages."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
from contextlib import ExitStack, closing
from dataclasses import replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from experiment_paths import DATA_ROOT, REPO

sys.path.insert(0, str(REPO))

from codey.agents.request import AgentRequest  # noqa: E402
from codey.app.context import AppContext  # noqa: E402
from codey.operations.ghost_context import ghost_control_enabled  # noqa: E402
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
from codey.providers.api_request_policy import LocalRequestPolicy, RequestLease, RetryDirective  # noqa: E402
from codey.providers.token_accounting import RequestContextCount  # noqa: E402
from codey.workspace.context_admission import GhostAdmissionResult, admit_ghost_context, request_scope  # noqa: E402
from codey.workspace.context_epoch import ContextEpoch, context_epoch_id  # noqa: E402

PLACEMENTS = ("kernel", "preparation", "attempt")
CASES = ("enabled", "disabled_initially", "lease_expiry", "lease_revoke", "wire_rewrite", "retry_expiry",
         "compaction_enabled", "compaction_lease_expiry", "unicode_enabled", "unicode_compaction")
ASCII_HINT = "Synthetic approved preference."
UNICODE_HINT = '以后请用中文。\nKeep "quoted" details.'
REWRITTEN = "Undeclared rewritten preference."
FILLER = "fixture-source: " + " ".join(f"bounded-{i:04d}" for i in range(800))


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def replace_hint(value, hint):
    if isinstance(value, str):
        return value.replace(hint, REWRITTEN)
    if isinstance(value, list):
        return [replace_hint(item, hint) for item in value]
    if isinstance(value, dict):
        return {key: replace_hint(item, hint) for key, item in value.items()}
    return value


def probe(directory, project, *, protocol, placement, case):
    directory.mkdir(parents=True, exist_ok=False)
    hint = UNICODE_HINT if case.startswith("unicode") else ASCII_HINT
    compacting = "compaction" in case
    expiring = case in {"lease_expiry", "retry_expiry", "compaction_lease_expiry"}
    payloads, wire_rows, gates, transitions, policy_rows = [], [], [], [], []
    session_id = "payload-placement"

    class Clock(datetime):
        current = datetime(2040, 1, 1, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    with closing(AppContext(directory / "state")) as state:
        if case == "disabled_initially" and not state.ghost_inbox.set_control(
            "admit_hints", False, scope="session", session_id=session_id,
        ):
            raise RuntimeError("initial disable failed")

        class Policy(LocalRequestPolicy):
            max_attempts = 2
            acquires = 0
            releases = 0

            def acquire(self, pending, context):
                self.acquires += 1
                if self.acquires == 1:
                    if case in {"lease_expiry", "compaction_lease_expiry"}:
                        Clock.current = datetime(2040, 1, 1, 0, 0, 2, tzinfo=UTC)
                        transitions.append("expired_at_first_lease")
                    if case == "lease_revoke":
                        ok = state.ghost_inbox.set_control(
                            "admit_hints", False, scope="session", session_id=session_id,
                        )
                        if not ok or state.ghost_inbox.control_enabled(
                            "admit_hints", project=str(project), session_id=session_id,
                        ):
                            raise RuntimeError("lease-time revocation failed")
                        transitions.append("revoked_at_first_lease")
                policy_rows.append({"event": "acquire", "purpose": context.purpose})

                def release():
                    self.releases += 1
                    policy_rows.append({"event": "release", "purpose": context.purpose})

                return RequestLease(release)

            def observe_failure(self, context, receipt, failure, budget):
                if case == "retry_expiry" and budget.used == 1:
                    Clock.current = datetime(2040, 1, 1, 0, 0, 2, tzinfo=UTC)
                    transitions.append("expired_before_retry")
                    return RetryDirective(True, reason="deterministic_503_fixture")
                return RetryDirective(False, reason="no_fixture_retry")

        policy = Policy()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                payloads.append(payload)
                values = list(strings(payload))
                text = "\n".join(values)
                auxiliary = any(value.startswith("Write terse work-state bullets:") for value in values)
                # This is the fixture oracle, not the candidate decision:
                # auxiliary sources contain JSON-escaped original text.
                visible = hint in text or json.dumps(hint, ensure_ascii=False)[1:-1] in text
                stale = (expiring and Clock.current.second >= 2) or "revoked_at_first_lease" in transitions
                failed_retry = case == "retry_expiry" and len(payloads) == 1
                wire_rows.append({"purpose": "compaction" if auxiliary else "conversation",
                                  "source_visible": visible, "stale": stale,
                                  "unsafe_source_sent": visible and stale, "rewritten_sent": REWRITTEN in text,
                                  "http_status": 503 if failed_retry else 200,
                                  "payload_epoch": context_epoch_id(json.dumps(payload, ensure_ascii=False))})
                if failed_retry:
                    body = b'{"error":{"message":"deterministic retry fixture"}}'
                elif auxiliary:
                    body = response("Goal: retain approved preference. Continue.")
                else:
                    body = response("done", done=True)
                self.send_response(503 if failed_retry else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def response(text, *, done=False):
            if protocol == "openai-completions":
                message = {"role": "assistant", "content": text}
                if done:
                    message = {"role": "assistant", "content": None, "tool_calls": [
                        {"id": "done-1", "type": "function", "function": {
                            "name": "done", "arguments": '{"summary":"done"}'}}]}
                result = {"choices": [{"message": message, "finish_reason": "stop"}]}
            else:
                output = [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}]
                if done:
                    output = [{"type": "function_call", "call_id": "done-1", "name": "done",
                               "arguments": '{"summary":"done"}'}]
                result = {"status": "completed", "output": output}
            return json.dumps(result).encode()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()

        def counter(payload, *, deadline):
            # Fixed pressure fixture: exercise real compaction orchestration,
            # not a claim about model tokenization or real capacity.
            values = list(strings(payload))
            auxiliary = any(value.startswith("Write terse work-state bullets:") for value in values)
            pressure = any("fixture-source:" in value for value in values) and not auxiliary
            return RequestContextCount(60000 if pressure else 40, "placement_pressure_fixture")

        def configure(payload):
            if case == "wire_rewrite":
                replaced = replace_hint(payload, hint)
                payload.clear()
                payload.update(replaced)

        provider = ApiProvider(f"http://127.0.0.1:{server.server_port}/v1", "synthetic-payload-placement",
                               native_tools=True, api_protocol=protocol, timeout=5, temperature=0, output_tokens=128,
                               context_window_tokens=65536, request_policy=policy, request_counter=counter,
                               configure_request=configure)
        enabled = ghost_control_enabled(state, "admit_hints", project=str(project), session_id=session_id)
        request = AgentRequest(provider=provider, project=project, task="Say done.", session_id=session_id,
                               ghost_directive=hint, permission_profile="planning_readonly", admit_hints=enabled)
        sources = _project_ghost_sources(request)
        if expiring:
            sources = tuple(replace(source, expires_at="2040-01-01T00:00:01Z") for source in sources)
        if compacting:
            provider.load_durable_context([
                {"role": "user", "content": hint + "\n" + FILLER},
                {"role": "assistant", "content": "previous accepted answer"},
                {"role": "user", "content": "recent canonical task"},
                {"role": "assistant", "content": "recent accepted answer"},
            ])

        def gate(client, payload):
            # Exactly one predicate owner in candidate cells. Text extraction
            # deliberately has no speculative decoder or lineage fallback.
            event = {"purpose": client.purpose, "at": Clock.current.isoformat(), "decision": "pending",
                     "payload_epoch": context_epoch_id(json.dumps(payload, ensure_ascii=False))}
            gates.append(event)
            try:
                admission = admit_ghost_context(
                    "\n".join(strings(payload)), sources, request_scope=request_scope(project, session_id),
                    require_all=True, admit_hints=ghost_control_enabled(
                        state, "admit_hints", project=str(project), session_id=session_id,
                    ),
                )
            except ValueError as exc:
                event.update(decision="reject", reason=str(exc))
                raise
            event.update(decision="accept", accepted=len(admission.accepted))

        prepare = ApiProvider._admit_request
        attempt = ApiProvider._generate_attempt

        def prepared(client, *args, **kwargs):
            payload = prepare(client, *args, **kwargs)
            gate(client, payload)
            return payload

        def attempted(client, payload, *args, **kwargs):
            gate(client, payload)
            return attempt(client, payload, *args, **kwargs)

        def no_kernel_predicate(prompt, *_args, **_kwargs):
            return GhostAdmissionResult(ContextEpoch(epoch_id=context_epoch_id(prompt)))

        try:
            with ExitStack() as stack:
                stack.enter_context(patch("codey.workspace.context_admission.datetime", Clock))
                if placement != "kernel":
                    stack.enter_context(patch("codey.operations.task_loop.admit_ghost_context", no_kernel_predicate))
                    method, replacement = ("_admit_request", prepared) if placement == "preparation" else ("_generate_attempt", attempted)
                    stack.enter_context(patch.object(ApiProvider, method, replacement))
                result = run_task_kernel(
                    TaskSession(policy=TaskPolicy(frozenset({"control"})), max_turns=1, coding_context_enabled=False),
                    request=KernelRunRequest(
                        transport=KernelTransportDeps(provider=provider, context_text=_project_context(request),
                            user_task=request.task, ghost_sources=sources, admit_hints=enabled,
                            context_scope=request_scope(project, session_id)),
                        execution=KernelExecutionDeps(project_path=project, session_id=session_id),
                    ),
                )
            (directory / "result.txt").write_text(repr(result), encoding="utf-8")
            if result.stop_reason not in {"done", "provider_failure"}:
                raise RuntimeError(f"unexpected probe outcome: {result.stop_reason}")
            if result.stop_reason == "provider_failure" and "Ghost context admission rejected" not in result.summary:
                raise RuntimeError(f"unexpected provider failure: {result.summary}")
            if policy.acquires != policy.releases:
                raise RuntimeError("admission rejection leaked request lease")
            stable = case in {"enabled", "disabled_initially", "unicode_enabled", "compaction_enabled", "unicode_compaction"}
            if placement == "kernel" and stable and result.stop_reason != "done":
                raise RuntimeError("baseline healthy control failed")
            return {"protocol": protocol, "placement": placement, "case": case,
                    "stop_reason": result.stop_reason, "requests": len(payloads),
                    "unsafe_source_requests": sum(row["unsafe_source_sent"] for row in wire_rows),
                    "rewritten_requests": sum(row["rewritten_sent"] for row in wire_rows),
                    "healthy_control_blocked": stable and result.stop_reason != "done",
                    "gates": gates, "wire": wire_rows, "transitions": transitions,
                    "lease_acquires": policy.acquires, "lease_releases": policy.releases,
                    "compaction_diagnostics": [{key: row.get(key) for key in ("kind", "committed")}
                                               for row in provider._compaction.diagnostics]}
        finally:
            (directory / "requests.json").write_text(json.dumps(payloads, indent=2), encoding="utf-8")
            (directory / "policy.json").write_text(json.dumps(policy_rows, indent=2), encoding="utf-8")
            provider.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", required=True)
    parser.add_argument("--metrics-out", type=Path)
    args = parser.parse_args()
    if Path(args.suite).name != args.suite or args.suite in {".", ".."}:
        parser.error("suite must be a directory name")
    root = DATA_ROOT / args.suite
    root.mkdir(parents=True, exist_ok=False)
    project = root / "shared-project"
    project.mkdir()
    manifest = {"suite": args.suite, "model": "synthetic-payload-placement", "endpoint": "ephemeral loopback HTTP",
                "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
                "started_at": datetime.now(UTC).isoformat(), "command": subprocess.list2cmdline(sys.argv),
                "placements": list(PLACEMENTS), "cases": list(CASES),
                "protocols": ["openai-completions", "openai-responses"], "cells": len(CASES) * 6, "reps": 1,
                "context": 65536, "reserve": 8192, "keep_recent": 12000, "output": 128, "temperature": 0,
                "timeout": 5, "max_turns": 1, "counter": "fixed pressure fixture; not real tokenizer",
                "production_changes": False, "candidate": "same validator moved; kernel predicate replaced, not stacked"}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    rows = []
    for protocol in manifest["protocols"]:
        for case in CASES:
            for placement in PLACEMENTS:
                cell = f"{protocol}-{case}-{placement}"
                try:
                    row = probe(root / cell, project, protocol=protocol, placement=placement, case=case)
                except Exception as exc:
                    (root / "failure.json").write_text(json.dumps({"cell": cell, "error": str(exc),
                        "at": datetime.now(UTC).isoformat()}), encoding="utf-8")
                    raise
                rows.append(row)
                print(json.dumps({key: row[key] for key in ("protocol", "case", "placement", "stop_reason", "requests",
                                                           "unsafe_source_requests", "healthy_control_blocked")}), flush=True)
    metrics = {"schema": "ghost_payload_admission_placement_v1", "config": {
        key: value for key, value in manifest.items() if key not in {"command", "started_at"}},
        "rows": rows, "decision": "EXPERIMENT_ONLY", "model_behavior_score": None}
    (root / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    if args.metrics_out:
        args.metrics_out.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
