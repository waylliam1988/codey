"""Cold and warm project-memory A/B with real headless history persistence."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from contextlib import closing, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.knowledge.store import KnowledgeStore
from codey.operations import knowledge_context
from codey.operations.task_context import ProjectTaskContextBuilder
from codey.operations.task_execution import build_research_tools, close_research_tools
from codey.providers import controls
from codey.providers.registry import connect_provider
from codey.workspace.facts import ProjectFactsStore
from tests.manual.coding_pipeline_cases import BY_NAME, CodingCase
from tests.manual.coding_pipeline_value_ab import (
    EvidenceProvider,
    event_rows,
    grade,
    save,
    source_metadata,
    write_files,
    writer,
)


def without_write_guidance(original):
    def render(tools, session, question):
        text = original(tools, session, question)
        if not session.policy.allows("knowledge.write") and text.startswith("Personal knowledge owner for this isolated knowledge space:"):
            return text.partition("\n")[2]
        return text
    return render


def followup(seed, files):
    if seed == "discount-bug":
        return CodingCase("basket-continuation", "Add basket_total(prices, percent) in pricing.py. "
            "Return the sum of discounted_price(price, percent) for each price; empty input returns 0. "
            "Preserve discounted_price behavior, including invalid percentage validation. Keep tests unchanged.",
            files, ("pricing.py",), ("pricing.py",), (
                ("normal", "from pricing import basket_total as f\nassert f([100,50],20)==120"),
                ("empty", "from pricing import basket_total as f\nassert f([],20)==0"),
                ("bounds", "from pricing import basket_total as f\nassert f([10,20],100)==0 and f([10,20],0)==30"),
                ("preserved_validation", "from pricing import discounted_price as f\ntry: f(10,101)\nexcept ValueError: pass\nelse: raise AssertionError('invalid accepted')")))
    return CodingCase("count-continuation", "Add count_values(values) to totals.py. Return how many "
        "items an iterable yields; support generators and empty iterables. Keep total, report.render "
        "and all existing tests unchanged.", files, ("totals.py",), ("totals.py",), (
            ("list", "from totals import count_values as f\nassert f([1,2,3])==3"),
            ("generator", "from totals import count_values as f\nassert f(x for x in range(4))==4"),
            ("empty", "from totals import count_values as f\nassert f(iter(()))==0"),
            ("preserved", "from totals import total\nfrom report import render\nassert total([2,3])==5 and render([-1,4])=='3'")))


def seed_history(provider, case, root, state):
    events = []
    mark = provider.mark()
    result = run_headless(
        HeadlessRequest(project=root, task=case.task, provider_id="local", max_turns=30,
                        session_id="memory-history", state_home=state,
                        research_store_root=state / "vault", project_changes_required=True),
        emit_jsonl=events.append, connect_provider=lambda *_a, **_k: provider,
        connect_reviewer=lambda *_a, **_k: provider,
    )
    quality = grade(root, case)
    facts = ProjectFactsStore(state).load(root)
    with closing(KnowledgeStore(state / "vault")) as store:
        notes = store.index.recent(20)
    return {"success": result.stop_reason == "done" and quality["quality_passed"] and bool(facts.successful_changes),
            "stop_reason": result.stop_reason, "events": events,
            "persisted_facts": ProjectFactsStore(state).render(root), "notes": notes,
            **quality, **provider.evidence(mark)}


def memory_row(provider, case, root, state, arm, phase):
    mark, started = provider.mark(), time.monotonic()
    row = {"case": case.name, "arm": arm, "phase": phase}
    events = []
    try:
        with closing(KnowledgeStore(state / "vault")) as store:
            enabled = arm != "none"
            builder = ProjectTaskContextBuilder(project_facts=ProjectFactsStore(state) if enabled else None,
                                               knowledge_store=store if enabled else None)
            def context():
                return builder.build(project=root, task=case.task, session_id="memory-history",
                    run_id="memory-followup", continue_task=False, provider_session_changed=False)
            prepared = context()
            row.update(facts=prepared.verified_facts, knowledge_context=prepared.knowledge_context,
                       candidate_sources=[vars(item) for item in prepared.verification_candidates])
            tools = build_research_tools(SimpleNamespace(knowledge_store=store if enabled else None),
                                         session_id="memory-history", project=str(root))
            try:
                treatment = (patch.object(knowledge_context, "knowledge_background",
                    without_write_guidance(knowledge_context.knowledge_background)) if arm == "memory_compact" else nullcontext())
                with treatment:
                    result, events = writer(provider, case, root, state / f"revision-{phase}-{arm}",
                        facts=prepared.verified_facts, research=prepared.research_context, research_tools=tools,
                        candidates=prepared.verification_candidates,
                        candidate_loader=lambda: context().verification_candidates, max_turns=24, events_out=events)
            finally:
                close_research_tools(tools)
            row.update(stop_reason=result.stop_reason, turns=result.turns, summary=result.summary)
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
    row.update(grade(root, case))
    row.update(events=event_rows(events), seconds=round(time.monotonic()-started, 3))
    row["success"] = row.get("stop_reason") == "done" and row["quality_passed"] and row["scope_ok"]
    row.update(provider.evidence(mark))
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase", action="append", choices=("cold", "warm"))
    parser.add_argument("--reverse", action="store_true")
    parser.add_argument("--arm", action="append", choices=("none", "memory", "memory_compact"))
    args = parser.parse_args()
    controls.begin_task_context("coding-memory-value-ab")
    provider = EvidenceProvider(connect_provider("local"))
    payload = {"model": provider.model, "rows": [], "histories": [], **source_metadata()}
    try:
        for phase in args.phase or ("cold", "warm"):
            names = ("discount-bug", "tag-feature") if phase == "cold" else ("discount-bug", "cross-file-rename")
            for name in names:
                with tempfile.TemporaryDirectory() as temporary:
                    root, state = Path(temporary) / "project", Path(temporary) / "state"
                    root.mkdir()
                    case = BY_NAME[name]
                    write_files(root, case.files)
                    if phase == "warm":
                        mark = provider.mark()
                        try:
                            history = seed_history(provider, case, root, state)
                        except Exception as exc:
                            history = {"success": False, "error": f"{type(exc).__name__}: {exc}",
                                       **grade(root, case), **provider.evidence(mark)}
                        payload["histories"].append({"case": name, **history})
                        save(args.output, payload)
                        if not history["success"]:
                            print(json.dumps({"history": name, "success": False}), flush=True)
                            continue
                        case = followup(name, grade(root, case)["files"])
                    backup = Path(temporary) / "baseline"
                    shutil.copytree(root, backup)
                    arms = args.arm or ("none", "memory")
                    for arm in reversed(arms) if args.reverse else arms:
                        assert root.resolve().parent == Path(temporary).resolve()
                        shutil.rmtree(root)
                        shutil.copytree(backup, root)
                        row = memory_row(provider, case, root, state, arm, phase)
                        payload["rows"].append(row)
                        save(args.output, payload)
                        print(json.dumps({key: row.get(key) for key in ("case", "phase", "arm", "success",
                            "quality_score", "stop_reason", "http_requests", "input_context_tokens", "error")}), flush=True)
    finally:
        provider.close()
        controls.end_task_context()
    return 0 if payload["rows"] and all(row["success"] for row in payload["rows"]) and all(
        item["success"] for item in payload["histories"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
