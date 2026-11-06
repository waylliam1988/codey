"""Deterministic paired overflow replay for the coverage candidate.

This is a no-model mechanism check.  Both arms receive the same observed
windows, passages and token weights; only the eviction function changes.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codey.research import answer_context as production
from tests.manual import research_window_coverage_candidate as candidate

CASES = (
    {
        "id": "two_versions_and_limit",
        "task": "Compare object versions V1 and V2, including limitations and exceptions.",
        "passages": {
            "a": ("V1 object alpha", "V1 specification", 7),
            "b": ("V2 object beta", "V2 specification", 7),
            "c": ("limitations and exceptions", "limits", 6),
            "d": ("background object", "background", 7),
        },
        "budget": 20,
    },
    {
        "id": "three_versions_and_limit",
        "task": "Compare versions V1, V2 and V3, including scope limits.",
        "passages": {
            "a": ("V1 object", "V1", 6),
            "b": ("V2 object", "V2", 6),
            "c": ("V3 object", "V3", 6),
            "d": ("scope limit", "limits", 5),
            "e": ("background context", "background", 6),
        },
        "budget": 24,
    },
)


def _run(case, arm):
    passages = {
        ref: SimpleNamespace(excerpt=excerpt, source=SimpleNamespace(title=title))
        for ref, (excerpt, title, _) in case["passages"].items()
    }
    weights = {ref: row[2] for ref, row in case["passages"].items()}
    session = SimpleNamespace(task_text=case["task"], _memory_results={})
    ledger = SimpleNamespace(read_passages=passages)
    scope = SimpleNamespace(aliases=tuple((f"e{i}", ref) for i, ref in enumerate(passages, 1)))

    def render(_session, _ledger, _scope, visible):
        return " ".join(ref for _, ref in visible)

    def count(text):
        return sum(weights[ref] for ref in text.split())

    function = production.answer_context if arm == "baseline" else candidate.answer_context
    with patch.object(production, "_render_context", render):
        result = function(session, ledger, scope, count_tokens=count, token_budget=case["budget"])
    return {"arm": arm, "overflow": True, "visible": result.split(), "token_count": count(result)}


def run(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=False)
    rows = []
    for case in CASES:
        pair = {arm: _run(case, arm) for arm in ("baseline", "candidate")}
        rows.append({"case": case["id"], "budget": case["budget"], "pair": pair})
    output = {"schema_version": 1, "kind": "paired_no_model_overflow_replay", "cases": rows}
    (root / "replay.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not all(row["pair"]["baseline"]["overflow"] and row["pair"]["candidate"]["overflow"] for row in rows):
        raise RuntimeError("paired overflow mechanism did not trigger")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    run(parser.parse_args().root)
