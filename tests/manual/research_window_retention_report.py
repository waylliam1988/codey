"""Inspect actual admitted requests; export separately entered rubric reviews."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tests.manual import research_window_retention_ab as ab


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []


def text_values(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(text_values(item) for item in value)
    if isinstance(value, dict):
        return "\n".join(text_values(v) for v in value.values())
    return ""


def inspect_run(root, result):
    case = next(c for c in ab.read(root / "fixtures.json")["cases"] if c["id"] == result["case"])
    name = f"{result['case']}-{result['seed']}-{result['arm']}"
    directory = root / "runs" / name
    views = lines(directory / "retention-views.jsonl")
    wire = lines(root / "wire" / (name + ".jsonl"))
    admitted = [r for r in wire if r.get("event") == "admitted_payload"]
    responses = [r for r in wire if r.get("event") == "generation_response"]
    exposures = []
    for request in admitted:
        sent = text_values(request["payload"]["messages"])
        matches = [i for i, view in enumerate(views) if view["renders"][-1]["text"] in sent]
        view = views[matches[-1]] if matches else None
        exposures.append({"request": request["index"], "matched_view": matches[-1] if matches else None,
            "overflow": view["overflow"] if view else None,
            "visible_labels": view["renders"][-1]["visible"] if view else None,
            "keys": [{"name": key["name"], "quote": key["quote"], "visible_in_actual_request": key["quote"] in sent,
                "read": any(key["quote"] in p["excerpt"] for p in view["read_passages"]) if view else None,
                "removed_from_view": bool(view and any(key["quote"] in p["excerpt"] for p in view["read_passages"])
                                          and key["quote"] not in view["renders"][-1]["text"])} for key in case["keys"]]})
    report_path = directory / "research-report.txt"
    events = lines(directory / "research-events.jsonl")
    tools = [r for r in events if r.get("type") == "tool"]
    return {**result, "report": report_path.read_text(encoding="utf-8") if report_path.exists() else "",
        "rubric": case["rubric"], "forbidden": case["forbidden"], "request_count": len(admitted),
        "model_input_tokens": sum((r.get("backend_perf") or {}).get("last_input_count", 0) for r in responses),
        "model_output_tokens": sum((r.get("backend_perf") or {}).get("last_token_count", 0) for r in responses),
        "tool_calls": [{k: r.get(k) for k in ("tool_name", "ok", "args")} for r in tools],
        "overflow_views": sum(view["overflow"] for view in views),
        "actual_request_exposures": exposures,
        "critical_removed_in_actual_requests": sum(key["removed_from_view"] and not key["visible_in_actual_request"]
                                                   for ex in exposures for key in ex["keys"]),
        "critical_never_read": [key["name"] for key in case["keys"]
            if not any(key["quote"] in p["excerpt"] for view in views for p in view["read_passages"])],
        "view_record_hash": hashlib.sha256((directory / "retention-views.jsonl").read_bytes()).hexdigest()
                            if (directory / "retention-views.jsonl").exists() else None}


def main():
    all_rows, specifications = [], {}
    for phase in ("dev", "holdout"):
        root = ab.phase_root(phase)
        if not root.exists():
            continue
        specifications[phase] = ab.read(root / "preregistration.json")
        for path in sorted((root / "results").glob("*.json")):
            row = inspect_run(root, ab.read(path))
            grades = root / "grades.json"
            if grades.exists():
                row["independent_review"] = ab.read(grades).get(path.stem)
            all_rows.append({"phase": phase, **row})
    ab.write(Path("docs/reports/research-window-retention-ab-20261106.json"), {
        "specifications": specifications, "runs": all_rows,
        "limitations": "Fixed materials and common 4096-token stress cap; no model self-grading, "
                       "no claim that a removed view equals absent evidence unless actual request confirms it."})
    for row in all_rows:
        print(row["phase"], row["case"], row["seed"], row["arm"], row["result"]["stop_reason"] if "result" in row else "error",
              "requests", row["request_count"], "overflow", row["overflow_views"],
              "key_removals", row["critical_removed_in_actual_requests"], "never_read", row["critical_never_read"])


if __name__ == "__main__":
    main()
