"""Strictly recompute the six canonical-checkpoint experiment arms."""
from __future__ import annotations

import argparse
import json
from itertools import product
from pathlib import Path

REPORTS = Path(__file__).resolve().parent / "reports"
PROTOCOLS = ("openai-completions", "openai-responses")
LIFETIMES = ("window_reopen", "app_restart")
ARMS = {"base": "baseline", "drop": "durable_replay", "edit": "canonical_edit"}


def summarize(matrices: dict) -> dict:
    expected = set(product(PROTOCOLS, LIFETIMES, (False, True)))
    indexed = {}
    configs = []
    for budget, arm in product(("full", "trim"), ARMS):
        matrix = matrices[budget, arm]
        config = matrix["config"]
        if (matrix["schema"] != "ghost_checkpoint_handoff_lifetime_v1"
                or config["cells"] != 8 or config["tasks_per_cell"] != 6 or config["reps"] != 1
                or config["handoff_source"] != ARMS[arm]
                or config["checkpoint_mode"] != ("full_replay" if budget == "full" else "trimmed_replay")):
            raise ValueError("INCOMPLETE: wrong checkpoint arm/configuration")
        configs.append({key: value for key, value in config.items()
                        if key not in {"suite", "handoff_source", "checkpoint_mode", "context", "reserve"}})
        rows = {}
        for row in matrix["rows"]:
            key = row["protocol"], row["lifetime"], row["revoke"]
            if key in rows or key not in expected:
                raise ValueError("INCOMPLETE: duplicate/unexpected checkpoint cell")
            if row["handoff_source"] != ARMS[arm] or row["checkpoint_mode"] != config["checkpoint_mode"]:
                raise ValueError("INCOMPLETE: mismatched checkpoint row")
            tasks = [row["first"], *row["middle"], row["second"]]
            if len(tasks) != 6 or len(row["plans"]) != 6 or row["final_task_index"] != 6:
                raise ValueError("INCOMPLETE: missing task/planner coverage")
            if any(task["http"] != 1 or task["stop_reason"] != "done" or task["provider_settlements"] != 2
                   or task["settled_methods"] != ["acknowledge_tool_results", "send_turn"] for task in tasks):
                raise ValueError("INCOMPLETE: missing actual send/settlement")
            if row["total_http"] != 6 + row["auxiliary_http"] or row["auxiliary_http"] != len(row["summary_sources"]):
                raise ValueError("INCOMPLETE: missing auxiliary HTTP accounting")
            accepted = [point for point in row["checkpoints"] if point["label"] == "accepted_checkpoint_before_lifecycle"]
            if (len(accepted) != 1 or not accepted[0]["accepted_checkpoints"]
                    or not accepted[0]["transport_has_fact"] or not accepted[0]["export_has_fact"]
                    or not any(source["has_fact"] for source in row["summary_sources"])):
                raise ValueError("INCOMPLETE: no accepted fact-carrying checkpoint")
            if accepted[0]["bounded_replay_has_fact"] != (budget == "full"):
                raise ValueError("INCOMPLETE: requested retained/trimmed source condition absent")
            if (not all(task["hint_sent"] for task in tasks[:-1])
                    or (not row["revoke"] and not row["second"]["hint_sent"])):
                raise ValueError("INCOMPLETE: healthy on control missing")
            if any(point["durable_entries_has_hint"] for point in row["checkpoints"]):
                raise ValueError("INCOMPLETE: injected hint contaminated canonical entries")
            edit = row["canonical_edit"]
            if arm == "edit":
                if (not edit or edit["status"] != "done" or not edit["source_has_fact"]
                        or not edit["summary_has_fact"] or edit["source_has_hint"] or edit["summary_has_hint"]):
                    raise ValueError("INCOMPLETE: invalid canonical source/edit")
            elif edit is not None:
                raise ValueError("INCOMPLETE: control arm unexpectedly wrote a canonical edit")
            rows[key] = row
        if rows.keys() != expected:
            raise ValueError("INCOMPLETE: missing cells/controls are not zeros")
        indexed[budget, arm] = rows
    if any(config != configs[0] for config in configs[1:]):
        raise ValueError("INCOMPLETE: unmatched commits or task configurations")
    results = []
    for budget, arm, lifetime in product(("full", "trim"), ARMS, LIFETIMES):
        cells = [indexed[budget, arm][protocol, lifetime, revoke]
                 for protocol, revoke in product(PROTOCOLS, (False, True))]
        results.append({"budget": budget, "arm": ARMS[arm], "lifetime": lifetime, "cells": 4,
                        "facts_sent": sum(row["second"]["fact_sent"] for row in cells),
                        "revoked_hint_sent": sum(row["second"]["hint_sent"] for row in cells if row["revoke"]),
                        "healthy_hint_sent": sum(row["second"]["hint_sent"] for row in cells if not row["revoke"]),
                        "main_http": 24, "auxiliary_http": sum(row["auxiliary_http"] for row in cells)})
    return {"schema": "ghost_checkpoint_unification_summary_v1", "decision": "EXPERIMENT_ONLY",
            "status": "INCOMPLETE", "cells": 48, "tasks": 288,
            "main_http": 288, "auxiliary_http": sum(row["auxiliary_http"] for row in results),
            "settled_task_effects": 576, "rows": results,
            "production_gaps": ["atomic source fence and queued/branch/delete behavior",
                                "warm consumption of canonical edits and native pending delivery",
                                "single semantic compaction producer and recorded auxiliary effects",
                                "live model safety/semantic/cost evidence; no production merge"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports", type=Path, default=REPORTS)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    matrices = {(budget, arm): json.loads((args.reports / f"checkpoint-{budget}-{arm}-metrics.json").read_text(encoding="utf-8"))
                for budget, arm in product(("full", "trim"), ARMS)}
    result = json.dumps(summarize(matrices), indent=2) + "\n"
    if args.out:
        args.out.write_text(result, encoding="utf-8")
    print(result, end="")


if __name__ == "__main__":
    main()
