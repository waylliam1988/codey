"""Live paired trials: omit only successful reads already represented by receipts.

Current production revision tools, guidance, evidence and guards stay identical.
Controlled prefixes settle actual tools before real inference; they are not LLM runs.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from tests.manual import research_branch_review as branch
from tests.manual import research_framework_comparison as framework
from tests.manual import research_lazy_revision_ab as freezing
from tests.manual import research_read_guidance_ab as runner

ARMS = ("baseline", "receipt_only", "search_only", "original_control", "production")


def original_history(session):
    """The committed 71f6bf3d presentation, retained as the post-integration control."""
    from codey.knowledge.model import canonical_json
    from codey.research.questions import INVESTIGATION_TOOLS

    counts, rows = {}, []
    for identity, result in session._memory_results.items():
        if result.call.name not in INVESTIGATION_TOOLS:
            continue
        key = canonical_json([result.call.name, result.call.args, result.ok, result.model_text])
        counts[key] = counts.get(key, 0) + 1
        rows.append({"result_ref": identity, "tool": result.call.name, "args": result.call.args,
                     "ok": result.ok, "identical_observations": counts[key],
                     **({"error": result.model_text[:500]} if not result.ok else {})})
    return rows[-8:]


def receipt_only_history(session, rows):
    """Conservative presentation only; never modify settled memory or receipts."""
    from codey.knowledge.model import canonical_json
    from codey.research.questions import INVESTIGATION_TOOLS

    seen = set()
    represented = set()
    for identity, result in session._memory_results.items():
        if not result.ok or result.call.name not in INVESTIGATION_TOOLS:
            return rows
        key = canonical_json([result.call.name, result.call.args, result.ok, result.model_text])
        if key in seen:
            return rows
        seen.add(key)
        if result.canonical.get("read_passages"):
            represented.add(identity)
    return [row for row in rows if row["result_ref"] not in represented]


def search_only_history(session):
    """Retain the original immediate-read view; compact only after knowledge search."""
    rows = original_history(session)
    latest = list(session._memory_results.values())[-1:]
    if latest and latest[0].call.name == "knowledge_search":
        return receipt_only_history(session, rows)
    return rows


@contextmanager
def treatment(arm):
    from codey.research import answer_context

    if arm not in ARMS:
        raise ValueError(arm)
    original = answer_context.investigation_history
    with ExitStack() as stack:
        if arm in {"receipt_only", "search_only", "original_control"}:
            stack.enter_context(patch.object(answer_context, "investigation_history", (
                lambda session: receipt_only_history(session, original(session))
            ) if arm == "receipt_only" else search_only_history if arm == "search_only" else original_history))
        yield


def worker(root, directory, base_url, case, seed, arm):
    with patch.object(branch, "ablation", treatment):
        branch.worker(root, directory, base_url, case, seed, arm)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "run", "worker"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--case")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--arm", choices=ARMS)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "worker":
        worker(root, args.directory, args.base_url, args.case, args.seed, args.arm)
        return
    if args.action == "freeze":
        freezing.freeze(root)
        print("FROZEN", root, flush=True)
        return
    branch.verify(root)
    spec = framework.read_json(root / "preregistration.json")
    rows = framework.read_json(root / "matrix.json") if (root / "matrix.json").exists() else []
    for index, item in enumerate(spec["matrix"]):
        if args.case and args.case != item["case"]:
            continue
        for seed in spec["seeds"]:
            if args.seed is not None and args.seed != seed:
                continue
            arms = item["arms"]
            shift = (index + seed) % len(arms)
            for arm in arms[shift:] + arms[:shift]:
                if args.arm and args.arm != arm:
                    continue
                if any((r["case"], r["seed"], r["arm"]) == (item["case"], seed, arm) for r in rows):
                    continue
                print("START", item["case"], seed, arm, flush=True)
                with patch.object(runner, "__spec__", __spec__):
                    row = runner.run_one(root, item["case"], seed, arm)
                rows.append(row)
                framework.write_json(root / "matrix.json", rows)
                print("END", item["case"], seed, arm, row.get("result"), flush=True)
                framework.backend_ready()


if __name__ == "__main__":
    main()
