"""Strict recomputation of canonical source transaction evidence."""
from __future__ import annotations

ARMS = ("unfenced", "revision", "head_revision")
CHANGES = ("none", "append", "branch", "edit", "recreate", "commit_append", "commit_recreate")
PROTOCOLS = ("openai-completions", "openai-responses")
SCHEMA = "ghost_canonical_source_fence_v1"


def summarize(metrics):
    if metrics.get("schema") != SCHEMA:
        raise ValueError("INCOMPLETE: source fence schema")
    config, rows = metrics["config"], metrics["rows"]
    expected = {(protocol, arm, change) for protocol in PROTOCOLS for arm in ARMS for change in CHANGES}
    if (not config.get("commit") or config.get("cells") != len(expected)
            or len(rows) != len(expected) or {(r["protocol"], r["arm"], r["change"]) for r in rows} != expected):
        raise ValueError("INCOMPLETE: missing or duplicate source controls")
    for row in rows:
        if (row["main_http"] != 5 or row["auxiliary_http"] < 1 or row["prelude_settlements"] != 10
                or row["prelude_tasks"] != 5 or row["final_task_ran"] is not False
                or row["final_hint_result"] is not None or row["canonical_source_has_fact"] is not True
                or row["canonical_source_has_hint"] is not False or row["source_entries"] <= 0
                or not row["source_digest"] or row["injected"] != (row["change"] != "none")
                or row["committed_edits"] != int(row["accepted"])
                or row["error"] != (None if row["accepted"] else "CommitConflict")):
            raise ValueError("INCOMPLETE: broken runtime or transaction evidence")
    return {"cells": len(rows), "tasks": sum(r["prelude_tasks"] for r in rows),
            "main_http": sum(r["main_http"] for r in rows),
            "auxiliary_http": sum(r["auxiliary_http"] for r in rows),
            "task_settlements": sum(r["prelude_settlements"] for r in rows),
            "outcomes": {arm: {change: {
                "accepted": sum(r["accepted"] for r in rows if r["arm"] == arm and r["change"] == change),
                "cells": sum(1 for r in rows if r["arm"] == arm and r["change"] == change),
            } for change in CHANGES} for arm in ARMS}, "decision": "EXPERIMENT_ONLY",
            "model_behavior_score": None}


