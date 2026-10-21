"""Measure Ghost ownership across real task/window/durable lifetimes (offline)."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from experiment_paths import DATA_ROOT, REPO

sys.path.insert(0, str(REPO))

from codey.app.context import AppContext  # noqa: E402
from codey.app.task_services import build_task_deps  # noqa: E402
from codey.durable.context import build_context  # noqa: E402
from codey.durable.model import thaw_json  # noqa: E402
from codey.operations.conversation_plan import plan_conversation  # noqa: E402
from codey.operations.task_entry import run_task_submission  # noqa: E402
from codey.providers.api_provider import ApiProvider  # noqa: E402
from codey.providers.token_accounting import RequestContextCount  # noqa: E402
from codey.task.model import StartTask, TaskSubmission  # noqa: E402

HINT = "Synthetic source lifetime preference ZXQ471."
MODEL = "synthetic-source-lifetime"
PROVIDER = "ghost-fixture"
SESSION = "source-lifetime"
LIFETIMES = ("warm", "window_reopen", "fresh_provider", "app_restart")
PROTOCOLS = ("openai-completions", "openai-responses")
WORK_FACT = "Synthetic early decision: preserve actuator code QZ592."
PADDING = "\n".join(f"Synthetic archived line {index:04d}: fixed observation, no instruction." for index in range(210))
CHECKPOINT_PRELUDE_TASKS = 5


def encode(value):
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def contains(value):
    return HINT in encode(value)


def fingerprint(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from strings(item)


def probe(directory: Path, *, protocol: str, lifetime: str, revoke: bool, handoff_source: str,
          checkpoint_mode: str = "none") -> dict:
    directory.mkdir(parents=True, exist_ok=False)
    project = directory / "project"
    project.mkdir()
    state_home = directory / "state"
    payloads, events, plans, checkpoints = [], [], [], []
    main_payloads, summary_sources, counter_rows = [], [], []
    providers = []
    state = AppContext(state_home)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            payloads.append(payload)
            auxiliary = any(value.startswith("<source>\n") for value in strings(payload))
            if auxiliary:
                source = "\n".join(value for value in strings(payload) if value.startswith("<source>\n"))
                summary_sources.append({"has_fact": WORK_FACT in source, "has_hint": HINT in source})
                # Only carry markers actually present in the historical source.
                summary = "Accepted work checkpoint. " + (WORK_FACT if WORK_FACT in source else "")
                summary += " " + (HINT if HINT in source else "")
                if protocol == "openai-completions":
                    reply = {"choices": [{"message": {"role": "assistant", "content": summary},
                                           "finish_reason": "stop"}]}
                else:
                    reply = {"status": "completed", "output": [{"type": "message", "role": "assistant",
                             "content": [{"type": "output_text", "text": summary}]}]}
            else:
                main_payloads.append(payload)
            call_id = f"done-{len(payloads)}"
            # Deliberately non-echoing reply: hint retention cannot come from the model.
            arguments = '{"summary":"Synthetic task completed."}'
            if not auxiliary and protocol == "openai-completions":
                reply = {"choices": [{"message": {"role": "assistant", "content": None,
                    "tool_calls": [{"id": call_id, "type": "function", "function": {
                        "name": "done", "arguments": arguments}}]}, "finish_reason": "stop"}]}
            elif not auxiliary:
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
        counting = {}
        if checkpoint_mode != "none":
            def counter(payload, *, deadline):
                count = (len(encode(payload)) + 3) // 4
                counter_rows.append({"estimated": count, "has_fact": WORK_FACT in encode(payload),
                                     "has_hint": contains(payload), "main_http_so_far": len(main_payloads)})
                return RequestContextCount(count, "estimated")
            counting["request_counter"] = counter
        window = 16384 if checkpoint_mode == "trimmed_replay" else 65536
        client = ApiProvider(endpoint, MODEL, api_protocol=protocol, native_tools=True,
                             timeout=5, temperature=0, output_tokens=128, context_window_tokens=window,
                             context_reserve_tokens=2048 if checkpoint_mode == "trimmed_replay" else 8192,
                             context_keep_recent_tokens=1000 if checkpoint_mode != "none" else 12000,
                             **counting)
        providers.append(client)
        return client

    provider = new_provider()

    def measured_plan(**kwargs):
        plan = plan_conversation(**kwargs)
        context = getattr(plan.window_action, "context", None)
        plans.append({"fresh_chat": plan.fresh_chat, "action": type(plan.window_action).__name__,
                      "handoff_has_hint": HINT in plan.handoff,
                      "replay_has_hint": context is not None and contains(list(context.messages)),
                      "replay_messages": len(context.messages) if context is not None else None,
                      "handoff_has_fact": WORK_FACT in plan.handoff,
                      "replay_has_fact": context is not None and WORK_FACT in encode(list(context.messages)),
                      "replay_omitted_entries": len(context.omitted_entry_ids) if context is not None else None})
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
        if checkpoint_mode != "none":
            bounded = state.durable.context_snapshot(SESSION, token_budget=provider.context_budget.input_limit,
                                                     token_counter=provider.count_context_messages)
            row.update(transport_has_fact=WORK_FACT in encode(provider._messages),
                       export_has_fact=WORK_FACT in provider.export_work_state(),
                       durable_entries_has_fact=any(WORK_FACT in str(entry) for entry in entries),
                       bounded_replay_has_fact=WORK_FACT in encode(list(bounded.messages)),
                       bounded_replay_omitted_entries=len(bounded.omitted_entry_ids),
                       accepted_checkpoints=len(provider.context_ledger.checkpoint_digests()))
        checkpoints.append(row)

    def submit(index):
        state.emit = events.append
        task = f"Say task {index} is complete."
        if checkpoint_mode != "none" and index <= CHECKPOINT_PRELUDE_TASKS:
            task += ("\n" + WORK_FACT if index == 1 else "") + "\n" + PADDING
        request = TaskSubmission.from_input(
            session_id=SESSION, project=str(project), max_turns=1, provider_id=PROVIDER,
            input=StartTask(task), intent="chat", run_id=f"source-run-{index}")
        deps = build_task_deps(state, connect_provider=lambda _provider_id: provider)
        start = len(main_payloads)
        def production_submission():
            with patch("codey.operations.ghost_context.ghost_directive", return_value=SimpleNamespace(text=HINT)), \
                 patch("codey.operations.task_phases.dispatch.plan_conversation", measured_plan):
                run_task_submission(deps, request)

        if handoff_source in {"durable_replay", "canonical_edit"}:
            # Candidate ablation only: the real planner still selects/replays
            # canonical SQLite context. Omit this additional export of its
            # transient protocol view; do not filter text or modify admission.
            with patch.object(provider, "export_work_state", return_value=""):
                production_submission()
        else:
            production_submission()
        terminal = [event for event in events if event.get("type") == "task_done"
                    and event.get("run_id") == request.run_id]
        if len(terminal) != 1 or terminal[0].get("stop_reason") != "done" or len(main_payloads) != start + 1:
            raise RuntimeError(f"incomplete task {index}: terminal={terminal!r}, main HTTP delta={len(main_payloads) - start}")
        if checkpoint_mode != "none":
            provider.wait_for_maintenance(5)
            maintenance = provider._compaction._thread
            if maintenance is not None and maintenance.is_alive():
                raise RuntimeError("background compaction did not finish; stop this cell")
        effects = state.runtime_effects.load_effects(SESSION, request.run_id)
        sends = [effect for effect in effects if effect.intent.effect_category == "provider_send"]
        if (sorted(effect.intent.display_ref for effect in sends) != ["acknowledge_tool_results", "send_turn"]
                or any(effect.settlement is None or effect.settlement.status != "ok" for effect in sends)):
            raise RuntimeError(f"missing actual successful provider settlement: {sends!r}")
        snapshot(f"after_task_{index}")
        if checkpoint_mode == "none" and index == 2 and ("Say task 1 is complete." not in encode(main_payloads[-1])
                           or "Synthetic task completed." not in encode(main_payloads[-1])):
            raise RuntimeError("second payload lost the prior canonical task/result markers")
        return {"http": len(main_payloads) - start, "stop_reason": terminal[0]["stop_reason"],
                "provider_settlements": len(sends), "settled_methods": sorted(effect.intent.display_ref for effect in sends),
                "hint_sent": contains(main_payloads[-1]), "payload_bytes": len(encode(main_payloads[-1]).encode()),
                "prior_task_sent": "Say task 1 is complete." in encode(main_payloads[-1]),
                "prior_result_sent": "Synthetic task completed." in encode(main_payloads[-1]),
                "fact_sent": WORK_FACT in encode(main_payloads[-1])}

    try:
        first = submit(1)
        if not first["hint_sent"]:
            raise RuntimeError("initial enabled control failed")
        middle = []
        canonical_edit = None
        final_index = 2
        if checkpoint_mode != "none":
            middle = [submit(index) for index in range(2, CHECKPOINT_PRELUDE_TASKS + 1)]
            provider.maintain_context()
            provider.wait_for_maintenance(5)
            maintenance = provider._compaction._thread
            if maintenance is not None and maintenance.is_alive():
                raise RuntimeError("explicit compaction did not finish; stop this cell")
            snapshot("accepted_checkpoint_before_lifecycle")
            if (not provider.context_ledger.checkpoint_digests()
                    or WORK_FACT not in provider.export_work_state()
                    or not any(row["has_fact"] for row in summary_sources)):
                raise RuntimeError("required accepted fact-carrying checkpoint was not produced")
            final_index = CHECKPOINT_PRELUDE_TASKS + 1
            if handoff_source == "canonical_edit":
                # Reuse the actual durable range-edit writer. The proposal
                # summarizes canonical entries, never the Ghost-bearing view.
                # A production binding/fence is deliberately not claimed.
                head = state.durable.head(SESSION)
                tail_input = state.durable.task(f"source-run-{CHECKPOINT_PRELUDE_TASKS}").state["input_entry_id"]
                path = list(state.durable.context(SESSION))
                tail_start = next(index for index, entry in enumerate(path) if entry.entry_id == tail_input)
                source_entries = [entry for entry in path[:tail_start]
                                  if entry.kind in {"user_message", "assistant_message", "tool_call", "tool_result"}]
                source = build_context(source_entries, token_budget=60000,
                                       token_counter=provider.count_context_messages)
                messages = [thaw_json(message) for message in source.messages]
                if contains(messages) or WORK_FACT not in encode(messages):
                    raise RuntimeError("canonical summary source is contaminated or missing the fact")
                summary = provider._summarize_context(provider._codec.encode_context(messages), time.monotonic() + 5)
                if state.durable.head(SESSION) != head:
                    raise RuntimeError("canonical source changed during summary; no edit submitted")
                edits = [{"target": source_entries[0].entry_id, "action": "replace",
                          "messages": [{"role": "assistant", "content": summary}]}]
                edits += [{"target": entry.entry_id, "action": "omit"} for entry in source_entries[1:]]
                result = state.durable.admit_write(
                    conversation_id=SESSION, request_id="checkpoint-canonical-edit", kind="context_edit",
                    data={"experiment": "checkpoint-unification", "source_digest": fingerprint(messages),
                          "source_entry_ids": [entry.entry_id for entry in source_entries],
                          "captured_revision": head[1]},
                    model={"provider_id": PROVIDER, "model_id": MODEL}, edits=edits, display_only=False)
                submission = state.durable.store.submission(str(result.submission_id))
                if str(submission.status) != "done":
                    raise RuntimeError("canonical context edit did not commit immediately")
                canonical_edit = {"source_entries": len(source_entries), "source_has_hint": contains(messages),
                                  "source_has_fact": WORK_FACT in encode(messages),
                                  "summary_has_hint": HINT in summary, "summary_has_fact": WORK_FACT in summary,
                                  "source_digest": fingerprint(messages), "status": str(submission.status)}
                snapshot("after_canonical_range_edit")
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
        snapshot(f"before_task_{final_index}")
        second = submit(final_index)
        if len(plans) != final_index:
            raise RuntimeError("did not traverse real conversation planning for every task")
        if lifetime == "warm" and (plans[-1]["fresh_chat"] or provider.window_ref != old_window):
            raise RuntimeError("warm control unexpectedly opened a window")
        if lifetime != "warm" and (not plans[-1]["fresh_chat"] or provider.window_ref == old_window):
            raise RuntimeError("lifecycle failed to open a distinct window")
        if not revoke and not second["hint_sent"]:
            raise RuntimeError("healthy on control lost current hint")
        return {"protocol": protocol, "lifetime": lifetime, "revoke": revoke, "handoff_source": handoff_source,
                "first": first, "second": second, "plans": plans, "checkpoints": checkpoints,
                "checkpoint_mode": checkpoint_mode, "middle": middle, "final_task_index": final_index,
                "summary_sources": summary_sources, "auxiliary_http": len(summary_sources),
                "total_http": len(payloads),
                "canonical_edit": canonical_edit,
                "revoked_hint_resent": revoke and second["hint_sent"],
                "window_changed": provider.window_ref != old_window}
    finally:
        (directory / "requests.json").write_text(encode(payloads), encoding="utf-8")
        (directory / "events.json").write_text(encode(events), encoding="utf-8")
        (directory / "plans.json").write_text(encode(plans), encoding="utf-8")
        (directory / "checkpoints.json").write_text(encode(checkpoints), encoding="utf-8")
        (directory / "compaction-diagnostics.json").write_text(encode([
            client._compaction.diagnostics for client in providers]), encoding="utf-8")
        (directory / "counter-rows.json").write_text(encode(counter_rows), encoding="utf-8")
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
    parser.add_argument("--handoff-source", choices=("baseline", "durable_replay", "canonical_edit"), default="baseline")
    parser.add_argument("--checkpoint", choices=("none", "full_replay", "trimmed_replay"), default="none")
    args = parser.parse_args()
    if args.handoff_source == "canonical_edit" and args.checkpoint == "none":
        parser.error("canonical_edit requires an explicit checkpoint scenario")
    if Path(args.suite).name != args.suite or args.suite in {".", ".."}:
        parser.error("suite must be a directory name")
    root = DATA_ROOT / args.suite
    root.mkdir(parents=True, exist_ok=False)
    manifest = {"suite": args.suite, "model": MODEL, "endpoint": "ephemeral loopback HTTP",
                "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
                "command": subprocess.list2cmdline(sys.argv), "started_at": datetime.now(UTC).isoformat(),
                "protocols": list(args.protocol), "lifetimes": list(args.lifetime),
                "handoff_source": args.handoff_source,
                "checkpoint_mode": args.checkpoint,
                "cells": len(args.protocol) * len(args.lifetime) * 2,
                "tasks_per_cell": 2 if args.checkpoint == "none" else CHECKPOINT_PRELUDE_TASKS + 1, "reps": 1,
                "on_off": "enabled prelude; enabled versus actual scoped disable before final task",
                "context": 16384 if args.checkpoint == "trimmed_replay" else 65536,
                "reserve": 2048 if args.checkpoint == "trimmed_replay" else 8192,
                "keep_recent": 12000 if args.checkpoint == "none" else 1000, "output_tokens": 128,
                "temperature": 0, "timeout_seconds": 5, "max_turns": 1,
                "hypothesis": "warm history and exported handoff retain hints absent from durable replay",
                "fixture": "fixed directive producer and non-echoing native done response; production gates unchanged",
                "counter": "normal provider estimate" if args.checkpoint == "none" else "ceil(serialized_chars/4), estimated",
                "summary": "none" if args.checkpoint == "none" else "carry fact/hint markers only if present in source"}
    (root / "manifest.json").write_text(encode(manifest), encoding="utf-8")
    rows = []
    for protocol in args.protocol:
        for lifetime in args.lifetime:
            for revoke in (False, True):
                cell = f"{protocol}-{lifetime}-{'revoke' if revoke else 'on'}"
                try:
                    row = probe(root / cell, protocol=protocol, lifetime=lifetime, revoke=revoke,
                                handoff_source=args.handoff_source, checkpoint_mode=args.checkpoint)
                except Exception as exc:
                    (root / "failure.json").write_text(encode({"cell": cell, "error": repr(exc),
                        "at": datetime.now(UTC).isoformat()}), encoding="utf-8")
                    raise
                rows.append(row)
                print(encode(row), flush=True)
    metrics = {"schema": "ghost_protocol_source_lifetime_v2" if args.checkpoint == "none"
               else "ghost_checkpoint_handoff_lifetime_v1", "config": {
        key: value for key, value in manifest.items() if key not in {"command", "started_at"}},
        "rows": rows, "decision": "EXPERIMENT_ONLY", "model_behavior_score": None}
    (root / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    if args.metrics_out:
        args.metrics_out.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
