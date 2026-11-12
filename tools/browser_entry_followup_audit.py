"""Audit paired browser-entry runs against actual requests and settled tool receipts."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from codey.types.json_value import JsonObject, decode_json_object
from tools import browser_entry_surface as entry
from tools.browser_production_live_gate import URL
from tools.completion_artifact_audit import tool_receipts

TOKEN = "SCHEMA_TOKEN_74"
RESULT = "[result: codemode]\n"
RESULT_DATA = RESULT + "Result data (JSON):\n"


def json_lines(path: Path) -> list[JsonObject]:
    return [decode_json_object(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines()]


def request_results(directory: Path) -> dict[int, list[str]]:
    requests: dict[int, list[str]] = {}
    for event in json_lines(directory / "provider.jsonl"):
        if event.get("phase") != "request":
            continue
        payload = decode_json_object(event["payload"])
        messages, exchange = payload["messages"], event["exchange"]
        if not isinstance(messages, list) or type(exchange) is not int:
            raise ValueError("unexpected provider request record")
        contents = [decode_json_object(message).get("content") for message in messages]
        requests[exchange] = [text.split("\n\nResult metadata:", 1)[0] for text in contents
                              if isinstance(text, str) and text.startswith(RESULT)]
    return requests


def audit(directory: Path, row: JsonObject) -> JsonObject:
    receipts = tool_receipts(directory / "state")
    by_ref = {receipt.identity: receipt.result for receipt in receipts}
    successful = [receipt.result for receipt in receipts if receipt.result.ok]
    requests = request_results(directory)
    candidate_calls: set[str] = set()
    for receipt in receipts:
        parent = receipt.result
        if parent.call.name != "codemode":
            continue
        refs = parent.canonical.get("child_effect_ids")
        if not isinstance(refs, list):
            continue
        leaves = {by_ref[ref].call.name for ref in refs if isinstance(ref, str) and ref in by_ref}
        methods = re.findall(r"\bbrowser\.(\w+)\s*\(", str(parent.call.args.get("code", "")))
        candidate_calls.update(method for method in methods
                               if {"property": "browser_eval", "url": "browser_tab_list"}.get(
                                   method, "browser_" + method) in leaves)

    url_read = token_read = facts_read = False
    for result in successful:
        value = result.canonical.get("value")
        if result.call.name == "browser_tab_list" and isinstance(value, dict):
            url_read |= value.get("page_url") == URL
        if result.call.name == "browser_eval" and isinstance(value, dict):
            token_read |= value.get("value") == TOKEN
        if result.call.name == "browser_read":
            facts_read |= all(fact in result.model_text for fact in ("173", "QUARTZ_851"))
    wire = [text for results in requests.values() for text in results if text.startswith(RESULT_DATA)]
    first_feedback = next((text.splitlines()[1] for results in requests.values() for text in results
                           if not text.startswith(RESULT_DATA) and len(text.splitlines()) > 1), None)
    events = json_lines(directory / "events.jsonl")
    recovery: JsonObject = {}
    if row["config"] == entry.WAIT_RECOVERY:
        parent = next(receipt.result for receipt in receipts if receipt.result.call.name == "codemode")
        scripted = decode_json_object(entry.diagnostic_action(
            entry.WAIT_RECOVERY, str(row["scenario"]), str(row["arm"]), URL)["args"])
        recovery = {"injected_call_matches": all(parent.call.args[key] == value for key, value in scripted.items()),
                    "first_valid_wait_turn": next((e.get("turn") for e in events if e.get("type") == "tool"
                        and e.get("tool_name") == "browser_wait" and e.get("ok") is True), None),
                    "first_browser_read_turn": next((e.get("turn") for e in events if e.get("type") == "tool"
                        and e.get("tool_name") == "browser_read" and e.get("ok") is True), None)}
    return {"scenario": row["scenario"], "arm": row["arm"], "passed": row["passed"],
            "stop": row.get("stop_reason"), "calls": len(requests), "candidate_calls": [name for name in sorted(candidate_calls)],
            "first_feedback": first_feedback, "stale_rejections": row.get("failed_stale_refs"),
            "exact_url_read": url_read, "exact_token_read": token_read, "business_facts_read": facts_read,
            "url_on_wire": url_read and any(URL in text for text in wire),
            "token_on_wire": token_read and any(TOKEN in text for text in wire),
            "business_facts_on_wire": facts_read and any("173" in text and "QUARTZ_851" in text for text in wire),
            **recovery}


def main(roots: list[str]) -> None:
    for name in roots:
        root = Path(name)
        report = next(root / file for file in ("summary.json", "interruption.json", "progress.json")
                      if (root / file).is_file())
        raw = decode_json_object(json.loads(report.read_text(encoding="utf-8")))["runs"]
        if not isinstance(raw, list):
            raise ValueError("run report must contain a list of runs")
        rows = []
        for item in raw:
            row = decode_json_object(item)
            directory = root / f'{row["repetition"]:02d}-{row["scenario"]}' / str(row["arm"])
            if "metrics" in row and (directory / "provider.jsonl").is_file():
                rows.append(audit(directory, row))
        (root / "followup-audit.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{root}: {len(rows)}/{len(raw)} units audited")


if __name__ == "__main__":
    main(sys.argv[1:])
