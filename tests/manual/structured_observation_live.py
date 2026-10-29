"""Real local-model requests through the production kernel and durable receipts.

Only the source search/fetch backend is a controlled fixture. No agent loop,
completion gate, executor, receipt writer, or recovery implementation is replaced.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codey.operations.recovery import record_entry_policy
from codey.operations.task_loop import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelRunRequest,
    KernelTransportDeps,
    run_task_kernel,
)
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.local_config import LocalContextBudget, LocalProviderConfig
from codey.providers.local_connection import connect_local
from codey.research.ledger import ResearchLedger
from codey.research.tools import ResearchTools
from codey.runtime.effects.effect_records import RuntimeEffectStore
from tests.manual.agent_stability_proxy import Proxy
from tests.support.receipt_fixtures import inspect_receipt_facts
from tests.support.session_projection import restore_task_session
from tests.test_session_log_receipt_recovery_preserves_facts import _dirs, _open_runtime

REQUESTED = "https://source-a.test/observation/requested"
FINAL = "https://source-a.test/observation/final"
BODY = "Observed marker: CERULEAN-742.\n" + "Controlled source filler.\n" * 400 + "tail-marker: AMBER-913.\n"


class SourceBackend:
    def __init__(self, case):
        self.case, self.calls = case, []

    def search(self, query, limit=8):
        self.calls.append(["search", query])
        return [] if self.case == "empty" else [{"url": REQUESTED, "title": "Fixture source", "snippet": "Open for content."}]

    def fetch(self, url):
        self.calls.append(["fetch", url])
        if self.case == "failure":
            return {"status": "error", "detail": "fixture backend unavailable; see https://help.test/manual", "url": url}
        return {"status": "ok", "url": FINAL, "title": "Observed source", "text": BODY, "truncated": False}


def probe(root, proxy, model, protocol, case):
    prompt = {
        "redirect": "Search for the fixture source, open its result, and report its observed marker and final URL. Use tools; do not infer the marker from memory.",
        "locator": "Open https://source-a.test/observation/requested. Then use source_search for tail-marker and open_hit on its locator. Report the tail marker from the opened text.",
        "empty": "Search once for nonexistent-fixture. If no results are returned, report that limitation and finish without inventing a source or marker.",
        "failure": "Open https://source-a.test/observation/requested once. If it fails, report the observed failure and finish without inventing source contents.",
    }[case]
    project, state, logdir = _dirs(root)
    _, mutations, managed, _, deps, sink = _open_runtime(logdir, state, "s", "r", project)
    required = case in {"redirect", "locator"}
    policy = TaskPolicy(frozenset({"control", "web.read"}), sources_open_required=required,
                        required_checks=(("research_sources_opened", "tool_succeeded:source_search", "tool_succeeded:open_hit")
                                         if case == "locator" else ("research_sources_opened",) if required else ()))
    record_entry_policy(mutations, session_id="s", run_id="r", policy=policy)
    session = TaskSession(policy=policy, task_kind="project", task_text=prompt, max_turns=8)
    backend = SourceBackend(case)
    tools = ResearchTools(search=backend, store=None, changes=None)
    config = LocalProviderConfig(base_url=f"http://127.0.0.1:{proxy.server_port}/v1", model=model,
                                 api_key="local", native_tools_mode=protocol,
                                 context=LocalContextBudget(32768, 1024, 12000), thinking_enabled=False)
    started = time.monotonic()
    before_requests = len(proxy.records)
    # Allow only this controlled backend's two fixture locators, not arbitrary destinations.
    def fixture_url(url, **kwargs):
        return None if url in {REQUESTED, FINAL} else "outside observation fixture"

    with closing(connect_local(config=config)) as provider, patch("codey.research.source_gateway.check_fetch_url", fixture_url):
        result = run_task_kernel(session, request=KernelRunRequest(
            transport=KernelTransportDeps(provider=provider, run_id="r",
                                          effect_scope="task", provider_id="local", user_task=prompt),
            execution=KernelExecutionDeps(research_tools=tools, managed_outputs=managed, session_id="s",
                                          durable=mutations.durable),
            observation=KernelObservationDeps(intent_sink=sink, completion_context={"research_ledger": tools.ledger}),
        ))
    calls_before_recovery = list(backend.calls)
    rows = RuntimeEffectStore(durable=mutations.durable).load_effects("s", "r")
    (root / "execution.json").write_text(json.dumps({
        "stop_reason": result.stop_reason, "summary": result.summary,
        "executed": [row.intent.tool_name for row in rows if row.intent.tool_name],
        "source_backend_calls": backend.calls,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    recovered = inspect_receipt_facts(deps, session_id="s", run_id="r", project="", task_kind="project")
    fresh, ledger = TaskSession(policy=policy), ResearchLedger()
    frame = SimpleNamespace(run_id="r", recovered_tool_outcomes=recovered.recovered_tool_outcomes,
                            settled_tool_outcomes=recovered.settled_tool_outcomes,
                            recovered_tool_result_batch_id=recovered.recovered_tool_result_batch_id)
    restore_task_session(frame, fresh, research_ledger=ledger)
    restored_once = (dict(fresh.search_results), set(fresh.opened_sources), len(ledger.searches))
    restore_task_session(frame, fresh, research_ledger=ledger)
    stable = restored_once == (dict(fresh.search_results), set(fresh.opened_sources), len(ledger.searches))
    facts_equal = (fresh.search_results == session.search_results and fresh.opened_sources == session.opened_sources
                   and ledger.opened_sources == tools.ledger.opened_sources and ledger.searches == tools.ledger.searches)
    from codey.operations.completion_gate import evaluate

    recovered_complete = evaluate(fresh, result.summary, context={"research_ledger": ledger}).complete
    executed = [row.intent.tool_name for row in rows if row.intent.tool_name]
    valid_case = (
        case == "redirect" and "web_search" in executed
        and bool({"open_result", "open_url"}.intersection(executed))
        and FINAL in session.opened_sources and "CERULEAN-742" in result.summary
        or case == "locator" and {"source_search", "open_hit"}.issubset(executed) and "AMBER-913" in result.summary
        or case == "empty" and "web_search" in executed and not session.search_results and not session.opened_sources
        or case == "failure" and "open_url" in executed and not session.opened_sources
    )
    result_row = {"case": case, "protocol": protocol, "ok": bool(result.completed and valid_case and recovered.ok
                  and stable and facts_equal and recovered_complete and backend.calls == calls_before_recovery),
                  "stop_reason": result.stop_reason, "summary": result.summary, "executed": executed,
                  "source_backend_calls": backend.calls, "facts_equal_after_recovery": facts_equal,
                  "repeat_recovery_stable": stable, "opened_sources": sorted(session.opened_sources),
                  "recovered_completion_passed": recovered_complete,
                  "proof": as_proof(result.proof), "seconds": round(time.monotonic() - started, 2),
                  "requests": proxy.records[before_requests:]}
    (root / "result.json").write_text(json.dumps(result_row, ensure_ascii=False, indent=2), encoding="utf-8")
    return {key: value for key, value in result_row.items() if key != "requests"}


def as_proof(proof):
    from dataclasses import asdict

    return asdict(proof) if proof is not None else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--upstream", default="http://127.0.0.1:5001")
    parser.add_argument("--model", required=True)
    parser.add_argument("--protocols", nargs="+", default=["on", "off"])
    parser.add_argument("--cases", nargs="+", default=["redirect", "locator", "empty", "failure"])
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    results = []
    proxy = Proxy(("127.0.0.1", 0), args.upstream, timeout=180, request_limit=100)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    try:
        for protocol in args.protocols:
            for case in args.cases:
                root = args.output / f"{protocol}-{case}"
                root.mkdir()
                try:
                    row = probe(root, proxy, args.model, protocol, case)
                except Exception as exc:
                    row = {"case": case, "protocol": protocol, "ok": False, "error": repr(exc)}
                    (root / "requests.json").write_text(json.dumps(proxy.records, ensure_ascii=False, indent=2), encoding="utf-8")
                results.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
                (args.output / "summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        proxy.shutdown()
        proxy.server_close()
    return 0 if all(row["ok"] for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
