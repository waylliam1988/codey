"""Record real search and local-model requests/receipts without changing production contracts."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.providers.local_discovery import probe_local_endpoint_detail
from codey.research.browser_search import BrowserSearchProvider
from codey.research.search_contracts import SearchState
from codey.types.json_value import JsonObject, decode_json_object, encode_transport_value
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import (
    ExperimentProvider,
    Target,
    append_json,
    production_source_digest,
    write_json,
)


def browser_case(directory: Path) -> bool:
    provider = BrowserSearchProvider(headless=False)
    records: list[JsonObject] = []
    try:
        for query in ("official Python pathlib documentation", "Python sqlite3 official documentation"):
            started = time.monotonic()
            outcome = provider.search(query, limit=4)
            record: JsonObject = {"query": query, "seconds": time.monotonic() - started,
                                  "outcome": encode_transport_value(outcome)}
            if provider.browser.resource_binding() is not None:
                record["page"] = decode_json_object(provider.browser.capture())
                observed = provider.browser.evaluate("document.documentElement.outerHTML.slice(0,1000000)", main_frame=True)
                record["html"] = observed.observation.get("value")
            records.append(record)
            write_json(directory / "browser.json", {"records": records})
            print(json.dumps({"query": query, "state": outcome.state, "seconds": record["seconds"]}), flush=True)
    finally:
        provider.close()
    return all(decode_json_object(record["outcome"])["state"] == SearchState.HITS for record in records)


def model_case(directory: Path, endpoint: str) -> bool:
    discovered, reason = probe_local_endpoint_detail(endpoint, timeout=5)
    if discovered is None or len(discovered.models) != 1:
        raise RuntimeError("local model preflight: " + reason)
    target = Target(discovered.base_url, discovered.models[0], 123456789, 180)
    provider = ExperimentProvider(target, directory, fixed_acquisition=False)
    events: list[JsonObject] = []

    def observe(event: dict[str, object]) -> None:
        row = decode_json_object(event)
        events.append(row)
        append_json(directory / "events.jsonl", row)

    def connect(provider_id: str, *, port: int) -> ExperimentProvider:
        return provider

    task = "请在网上搜索 Python pathlib 官方文档，打开官方页面，说明 Path.exists() 的用途，并附上来源链接。"
    write_json(directory / "input.json", {"task": task, "model": target.model, "base_url": target.base_url})
    try:
        result = run_headless(HeadlessRequest(None, task, intent="chat", provider_id="local", max_turns=10,
            state_home=directory / "state", research_store_root=directory / "knowledge",
            requested_capabilities=("web.read",),
            model_selection={"model": target.model, "base_url": target.base_url}),
            emit_jsonl=observe, connect_provider=connect)
    finally:
        provider.close()
    receipts = [item.result for item in tool_receipts(directory / "state")]
    calls: list[JsonObject] = [{"tool": item.call.name, "ok": item.ok,
                              "canonical": decode_json_object(item.canonical),
                              "audit": decode_json_object(item.audit)} for item in receipts]
    passed = (result.stop_reason == "done" and any(item.call.name == "web_search" and item.ok for item in receipts)
              and any(item.call.name == "open_url" and item.ok for item in receipts))
    write_json(directory / "model-summary.json", {"passed": passed, "stop_reason": result.stop_reason,
                                                 "receipts": calls})
    return passed


class Arguments(argparse.Namespace):
    run_dir: Path
    model: bool
    endpoint: str


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--model", action="store_true")
    parser.add_argument("--endpoint", default="http://127.0.0.1:5001/v1")
    args = parser.parse_args(namespace=Arguments())
    args.run_dir.mkdir(parents=True, exist_ok=False)
    before = production_source_digest()
    passed = model_case(args.run_dir, args.endpoint) if args.model else browser_case(args.run_dir)
    after = production_source_digest()
    passed = passed and before == after
    write_json(args.run_dir / "summary.json", {"passed": passed, "mode": "model" if args.model else "browser",
        "source_before": before, "source_after": after, "source_stable": before == after})
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
