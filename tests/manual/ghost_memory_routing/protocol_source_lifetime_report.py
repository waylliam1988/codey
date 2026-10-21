"""Recompute the complete matched source-lifetime matrix without raw requests."""
from __future__ import annotations

import argparse
import json
from itertools import product
from pathlib import Path

REPORTS = Path(__file__).resolve().parent / "reports"
LIFETIMES = ("warm", "window_reopen", "fresh_provider", "app_restart")
PROTOCOLS = ("openai-completions", "openai-responses")


def validate(metrics: dict, arm: str) -> dict:
    if metrics.get("schema") != "ghost_protocol_source_lifetime_v2":
        raise ValueError("INCOMPLETE: expected the marker-checked v2 matrix")
    config = metrics["config"]
    if config["handoff_source"] != arm or config["cells"] != 16 or config["reps"] != 1:
        raise ValueError("INCOMPLETE: wrong arm or sample configuration")
    expected = set(product(PROTOCOLS, LIFETIMES, (False, True)))
    indexed = {}
    for row in metrics["rows"]:
        key = row["protocol"], row["lifetime"], row["revoke"]
        if key not in expected or key in indexed:
            raise ValueError("INCOMPLETE: duplicate or unexpected cell")
        if row["handoff_source"] != arm:
            raise ValueError("INCOMPLETE: row/config arm mismatch")
        for task in (row["first"], row["second"]):
            if (task["http"] != 1 or task["stop_reason"] != "done" or task["provider_settlements"] != 2
                    or task["settled_methods"] != ["acknowledge_tool_results", "send_turn"]):
                raise ValueError("INCOMPLETE: task/HTTP/settlement coverage missing")
        if not row["first"]["hint_sent"] or (not row["revoke"] and not row["second"]["hint_sent"]):
            raise ValueError("INCOMPLETE: healthy on control missing")
        if not row["second"]["prior_task_sent"] or not row["second"]["prior_result_sent"]:
            raise ValueError("INCOMPLETE: prior canonical markers missing")
        if row["revoked_hint_resent"] != (row["revoke"] and row["second"]["hint_sent"]):
            raise ValueError("INCOMPLETE: inconsistent revoked send observation")
        if len(row["plans"]) != 2 or len(row["checkpoints"]) != 3:
            raise ValueError("INCOMPLETE: missing ownership/lifecycle observation")
        indexed[key] = row
    if indexed.keys() != expected:
        raise ValueError("INCOMPLETE: missing cells; absent controls are not zero")
    return indexed


def summarize(baseline: dict, replay: dict) -> dict:
    left = validate(baseline, "baseline")
    right = validate(replay, "durable_replay")
    config_left = {key: value for key, value in baseline["config"].items() if key not in {"suite", "handoff_source"}}
    config_right = {key: value for key, value in replay["config"].items() if key not in {"suite", "handoff_source"}}
    if config_left != config_right:
        raise ValueError("INCOMPLETE: unmatched experiment configurations or commits")
    rows = []
    for lifetime in LIFETIMES:
        row = {"lifetime": lifetime, "revoked_cells_per_arm": 2, "on_cells_per_arm": 2}
        for label, indexed in (("baseline", left), ("durable_replay", right)):
            revoked = [indexed[protocol, lifetime, True] for protocol in PROTOCOLS]
            on = [indexed[protocol, lifetime, False] for protocol in PROTOCOLS]
            row[f"{label}_revoked_hint_sent"] = sum(cell["revoked_hint_resent"] for cell in revoked)
            row[f"{label}_healthy_hint_sent"] = sum(cell["second"]["hint_sent"] for cell in on)
            row[f"{label}_revoked_handoff_has_hint"] = sum(cell["plans"][1]["handoff_has_hint"] for cell in revoked)
        rows.append(row)
    all_cells = list(left.values()) + list(right.values())
    return {"schema": "ghost_protocol_source_lifetime_summary_v1", "decision": "EXPERIMENT_ONLY",
            "candidate_status": "INCOMPLETE", "cells_per_arm": 16, "tasks_per_arm": 32,
            "http_per_arm": 32, "settled_effects_per_arm": 64,
            "all_second_tasks_retain_prior_task_and_result_markers": True,
            "durable_entries_with_hint": sum(point["durable_entries_has_hint"]
                for cell in all_cells for point in cell["checkpoints"]),
            "durable_contexts_with_hint": sum(point["durable_context_has_hint"]
                for cell in all_cells for point in cell["checkpoints"]),
            "rows": rows,
            "limits": ["one deterministic repetition per cell; no live model",
                       "window_reopen is registry invalidation, not token-pressure rollover",
                       "prior markers do not establish arbitrary checkpoint/compaction semantic parity",
                       "serialized byte sizes include different fixture paths; no isolated token/cost/latency claim",
                       "warm history still resends revoked hints; no production merge"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=REPORTS / "protocol-source-lifetime-baseline-metrics.json")
    parser.add_argument("--replay", type=Path, default=REPORTS / "protocol-source-lifetime-replay-metrics.json")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    summary = summarize(json.loads(args.baseline.read_text(encoding="utf-8")),
                        json.loads(args.replay.read_text(encoding="utf-8")))
    text = json.dumps(summary, indent=2) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
