"""Fixed, controlled cross-domain probes through the production kernel.

Use this identical file in each checkout, serially against the same local
model. No model judge, custom agent loop, or performance acceptance threshold.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import threading
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codey.operations.kernel_session_recovery import restore_task_session
from codey.operations.recovery import record_entry_policy, recover_effects_for_resume
from codey.operations.task_effects import KernelRecordedProvider
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
from tests.test_session_log_receipt_recovery_preserves_facts import _dirs, _open_runtime

URL = "https://evaluation.test/requested"
FINAL = "https://evaluation.test/final"
SCENARIOS = (
    {"id": "geology", "domain": "science", "sources": True,
     "prompt": f"Open {URL}. Report the measured sample code and density, citing the final URL. Use the observed fixture, not general knowledge.",
     "body": "Laboratory observation: sample BOREAL-673 has measured density 2.73 g/cm3.",
     "tokens": ["BOREAL-673", "2.73"], "tools": ["open_url"]},
    {"id": "software", "domain": "software", "sources": True,
     "prompt": "Search for the AuroraTools migration fixture, open its result, then report the migration code and replacement configuration key. Cite the final source URL.",
     "body": "AuroraTools 3.8 migration notice. Migration code JADE-428. Replace cache_mode with cache_policy.",
     "tokens": ["JADE-428", "cache_policy"], "tools": ["web_search", "open_url"]},
    {"id": "statistics", "domain": "public_statistics", "sources": True,
     "prompt": f"Open {URL}. Report the observed count, observation date and calibration code. Cite the source and do not change its historical observation date to today.",
     "body": "Cedar Observatory record dated 2024-05-22: sensor 7 count was 1842; calibration code LILAC-592.",
     "tokens": ["1842", "2024-05-22", "LILAC-592"], "tools": ["open_url"]},
    {"id": "long_source", "domain": "long_document", "sources": True,
     "prompt": f"Open {URL}. Report the document code and cite its final source URL.",
     "body": "Document code QUARTZ-851.\n" + "Archived observations remain historical data.\n" * 300,
     "tokens": ["QUARTZ-851"], "tools": ["open_url"]},
    {"id": "zero_results", "domain": "missing_evidence", "sources": False, "mode": "empty",
     "prompt": "Search once for nonexistent-fixture. If the search returns no results, report that limitation and finish without inventing a source.",
     "tokens": [], "tools": ["web_search"]},
    {"id": "read_failure", "domain": "failed_evidence", "sources": False, "mode": "failure",
     "prompt": f"Open {URL} once. If it fails, report the observed error and finish without inventing source contents.",
     "tokens": ["fixture backend unavailable"], "tools": ["open_url"]},
    {"id": "explanation", "domain": "explanation", "sources": False,
     "prompt": "Explain a stack in one short sentence using the term LIFO. No tools are needed.",
     "tokens": ["LIFO"], "tools": []},
    {"id": "creation", "domain": "creative_writing", "sources": False,
     "prompt": "写一句包含“月亮”和“灯”的原创童话开头。直接回答，不需要工具。",
     "tokens": ["月亮", "灯"], "tools": []},
    {"id": "fixed_json", "domain": "structured_output", "sources": False,
     "prompt": 'Compute 17 times 19. Return exactly one JSON object {"product":NUMBER}, without Markdown or explanation. No tools are needed.',
     "tokens": [], "tools": [], "json": {"product": 323}},
)


class Backend:
    def __init__(self, scenario):
        self.scenario, self.calls = scenario, []

    def search(self, query, limit=8):
        self.calls.append(["search", query])
        return [] if self.scenario.get("mode") == "empty" else [{"url": URL, "title": "Observed fixture", "snippet": "Open for the actual record."}]

    def fetch(self, url):
        self.calls.append(["fetch", url])
        if self.scenario.get("mode") == "failure":
            return {"status": "error", "detail": "fixture backend unavailable; see https://help.test/manual", "url": url}
        return {"status": "ok", "url": FINAL, "title": "Observed fixture", "text": self.scenario.get("body", "No record"), "truncated": False}


def score(scenario, row):
    summary = row["summary"]
    answers_correct = all(token.lower() in summary.lower() for token in scenario["tokens"])
    if "json" in scenario:
        try:
            answers_correct = json.loads(summary) == scenario["json"]
        except ValueError:
            answers_correct = False
    source_ok = row["opened_sources"] == ([FINAL] if scenario["sources"] else [])
    tools_ok = set(scenario["tools"]).issubset(row["executed"])
    citation_ok = not scenario["sources"] or FINAL in summary
    unnecessary = not scenario["tools"] and bool(row["executed"])
    return {"answer_ok": bool(row["completed"] and answers_correct and source_ok and tools_ok and citation_ok and not unnecessary),
            "citation_ok": citation_ok,
            "unnecessary_source_calls": unnecessary,
            "recovery_ok": bool(row.get("recovery_ok"))}


def probe(root, proxy, model, protocol, scenario):
    project, state, logdir = _dirs(root)
    _, mutations, managed, _, deps, sink = _open_runtime(logdir, state, "s", "r", project)
    policy = TaskPolicy(frozenset({"control", "web.read"}), sources_open_required=scenario["sources"],
                        required_checks=("research_sources_opened",) if scenario["sources"] else ())
    record_entry_policy(mutations, session_id="s", run_id="r", policy=policy)
    session = TaskSession(policy=policy, task_kind="project", task_text=scenario["prompt"], max_turns=8)
    backend = Backend(scenario)
    tools = ResearchTools(search=backend, store=None, changes=None)
    config = LocalProviderConfig(base_url=f"http://127.0.0.1:{proxy.server_port}/v1", model=model,
                                 api_key="local", native_tools_mode=protocol,
                                 context=LocalContextBudget(32768, 1024, 12000), thinking_enabled=False)
    before = len(proxy.records)
    with closing(connect_local(config=config)) as provider, patch("codey.research.source_gateway.check_fetch_url",
        lambda url, **kw: None if url in {URL, FINAL} else "outside evaluation fixture"):
        result = run_task_kernel(session, request=KernelRunRequest(
            transport=KernelTransportDeps(provider=KernelRecordedProvider(provider, sink), run_id="r",
                                          effect_scope="task", provider_id="local", user_task=scenario["prompt"]),
            execution=KernelExecutionDeps(research_tools=tools, managed_outputs=managed,
                                          session_id="s", durable=mutations.durable),
            observation=KernelObservationDeps(intent_sink=sink, completion_context={"research_ledger": tools.ledger}),
        ))
    rows = RuntimeEffectStore(durable=mutations.durable).load_effects("s", "r")
    row = {"id": scenario["id"], "domain": scenario["domain"], "protocol": protocol,
           "completed": result.completed, "stop_reason": result.stop_reason, "summary": result.summary,
           "opened_sources": sorted(session.opened_sources), "source_backend_calls": list(backend.calls),
           "executed": [effect.intent.tool_name for effect in rows if effect.intent.tool_name],
           "proof": asdict(result.proof) if result.proof is not None else None}
    try:
        recovered = recover_effects_for_resume(deps, session_id="s", run_id="r", project="", task_kind="project")
        frame = SimpleNamespace(run_id="r", recovered_tool_outcomes=recovered.recovered_tool_outcomes,
            settled_tool_outcomes=recovered.settled_tool_outcomes, recovered_tool_result_batch_id=recovered.recovered_tool_result_batch_id)
        fresh, ledger = TaskSession(policy=policy), ResearchLedger()
        restore_task_session(frame, fresh, research_ledger=ledger)
        restore_task_session(frame, fresh, research_ledger=ledger)
        row["recovery_ok"] = bool(recovered.ok and fresh.search_results == session.search_results
            and fresh.opened_sources == session.opened_sources and ledger.searches == tools.ledger.searches
            and ledger.opened_sources == tools.ledger.opened_sources and backend.calls == row["source_backend_calls"])
    except Exception as exc:
        row.update(recovery_ok=False, recovery_error=repr(exc))
    row.update(score(scenario, row))
    row["requests"] = proxy.records[before:]
    (root / "result.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    return {key: value for key, value in row.items() if key != "requests"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--upstream", default="http://127.0.0.1:5001")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    proxy = Proxy(("127.0.0.1", 0), args.upstream, timeout=180, request_limit=200)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    manifest = {"revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "model": args.model, "protocols": ["on", "off"],
                "suite_sha256": hashlib.sha256(json.dumps(SCENARIOS, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                "scenarios": SCENARIOS, "results": []}
    try:
        for protocol in manifest["protocols"]:
            for scenario in SCENARIOS:
                root = args.output / f"{protocol}-{scenario['id']}"
                root.mkdir()
                row = probe(root, proxy, args.model, protocol, scenario)
                manifest["results"].append(row)
                (args.output / "summary.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        proxy.shutdown()
        proxy.server_close()
    return 0 if all(row["answer_ok"] and row["recovery_ok"] for row in manifest["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
