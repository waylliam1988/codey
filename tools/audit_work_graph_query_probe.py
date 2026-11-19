"""Audit saved live results without changing original summaries or fixtures."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from codey.types.json_value import JsonObject, JsonValue, decode_json_object
from tools import agent_work_graph_probe as pilot
from tools.work_graph_candidate import TOOL_NAME


def current_pytest_pass(checks: list[JsonObject]) -> bool:
    tests = [check for check in checks if "pytest" in str(check.get("command", ""))]
    latest: JsonObject = tests[-1] if tests else {}
    return bool(latest.get("current") and latest.get("ok") and latest.get("exit_code") == 0
                and str(latest.get("command", "")).strip() == "python -m pytest -q"
                and re.search(r"\d+ passed", str(latest.get("output", ""))))


def audit(root: Path) -> JsonObject:
    rows: list[JsonObject] = []
    for summary in sorted(root.glob("r*-*/summary.json")):
        directory = summary.parent
        row = decode_json_object(json.loads(summary.read_text(encoding="utf-8")))
        raw_checks: object = json.loads((directory / "production-checks.json").read_text(encoding="utf-8"))
        if not isinstance(raw_checks, list):
            raise ValueError("Expected an array of production execution checks")
        checks = [decode_json_object(check) for check in raw_checks]
        nodes = decode_json_object(decode_json_object(json.loads(
            (directory / "final-graph.json").read_text(encoding="utf-8")))["nodes"])
        corrected: list[JsonValue] = []
        for check in checks:
            if not check.get("output"):
                key = "execution:" + str(check["effect_id"])
                node = decode_json_object(nodes[key])
                # This artifact was built from the same committed ToolResult.
                # Restore only text; never derive trusted identity or exit from it.
                text = str(node["text"]).split("\n", 2)
                if len(text) != 3:
                    raise ValueError("Missing saved production observation body")
                check["output"] = text[2]
                corrected.append(key)
        verified = current_pytest_pass(checks)
        row["original_production_current_pytest_pass"] = row["production_current_pytest_pass"]
        row["original_success"] = row["success"]
        row["production_current_pytest_pass"] = verified
        row["success"] = bool(row["objective_success"] and row["stop_reason"] == "done"
                              and verified and row.get("exception") is None)
        row["output_corrections_from_saved_observations"] = corrected
        row["query_call_count"] = sum(event.get("type") == "tool" and event.get("tool_name") == TOOL_NAME
                                      for event in pilot.records(directory / "events.jsonl"))
        raw_receipts: object = json.loads((directory / "query-receipts.json").read_text(encoding="utf-8"))
        if not isinstance(raw_receipts, list):
            raise ValueError("Expected durable query receipt array")
        row["durable_successful_query_count"] = sum(decode_json_object(receipt).get("ok") is True for receipt in raw_receipts)
        rows.append(row)
    arms = {}
    for arm in ("baseline", "flat", "graph"):
        group = [row for row in rows if row["arm"] == arm]
        arms[arm] = {"n": len(group), "product_success": sum(bool(row["product_success"]) for row in group),
                     "objective_success": sum(bool(row["objective_success"]) for row in group),
                     "success": sum(bool(row["success"]) for row in group),
                     "query_calls": sum(int(str(row["query_call_count"])) for row in group)}
    result = {"method": "Saved committed observations restore missing display text only; original summaries are preserved.",
              "arms": arms, "rows": rows}
    pilot.write(root / "audited-results.json", result)
    return decode_json_object(result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(audit(args.root)["arms"], ensure_ascii=False))


if __name__ == "__main__":
    main()
