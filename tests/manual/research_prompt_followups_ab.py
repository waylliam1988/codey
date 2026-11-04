"""Independent prompt-only follow-ups on the production Research kernel."""

from __future__ import annotations

import argparse
import json
import shutil
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.manual import research_branch_review as branch
from tests.manual import research_framework_comparison as framework
from tests.manual import research_guidance_simplification_ab as shared
from tests.manual.research_read_guidance_ab import rewrite_recall

ARMS = ("baseline", "recall_header", "read_at_use", "next_read", "merge_task", "merge_conditions", "methods_short")


def rewrite_navigation(text: str) -> str:
    parts = text.split("\n\n")
    for index, part in enumerate(parts):
        if not part.startswith('{"investigation_attempts":'):
            continue
        view = json.loads(part)
        for row in view["source_locators"]:
            offset = row.get("next_offset")
            if type(offset) is not int or not 0 <= offset < row.get("available_length", 0):
                continue
            if "knowledge_source" in row:
                identity, revision = row["knowledge_source"]
                row["next_read"] = {"tool": "knowledge_read", "args": {
                    "id": identity, "revision": revision, "offset": offset}}
            elif "url" in row:
                row["next_read"] = {"tool": "open_url", "args": {"url": row["url"], "offset": offset}}
        parts[index] = json.dumps(view, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # One actionable explanation, while retaining all evidence/trust rules.
    return "\n\n".join(parts) + (
        "\nBefore reporting a source gap, inspect the relevant unread sections. "
        "next_read reads the first unread range; use source_search for a named section. "
        "An unread range is not evidence that the source lacks the answer."
    )


@contextmanager
def treatment(arm: str):
    from codey.operations import knowledge_context
    from codey.research import answer_context, answer_contract

    if arm not in ARMS:
        raise ValueError(arm)
    with ExitStack() as stack:
        if arm == "recall_header":
            original = knowledge_context.render_recall

            def recalled(*args, **kwargs):
                result = original(*args, **kwargs)
                return rewrite_recall(result, short=True) if kwargs.get("compact") else result

            stack.enter_context(patch.object(knowledge_context, "render_recall", recalled))
        if arm == "read_at_use":
            original = answer_contract.answer_spec

            def spec_at_use(spec, scope):
                result = original(spec, scope)
                if result.name == "knowledge_read":
                    suffix = " " + answer_contract.KNOWLEDGE_READ_GUIDANCE
                    if not result.description.endswith(suffix):
                        raise ValueError("knowledge_read guidance differs from the declared baseline")
                    result = replace(result, description=result.description[:-len(suffix)])
                return result

            stack.enter_context(patch.object(answer_contract, "answer_spec", spec_at_use))
        if arm == "next_read":
            original = answer_context.answer_context

            def view(*args, **kwargs):
                return rewrite_navigation(original(*args, **kwargs))

            stack.enter_context(patch.object(answer_context, "answer_context", view))
        if arm in {"merge_task", "merge_conditions"}:
            original = answer_contract.ANSWER_GUIDANCE
            before, after = {
                "merge_task": (
                    "Research the user's actual questions, not just their general topic. "
                    "Answer the complete original task in the user's requested language and question order. ",
                    "Answer every actual question in the complete original task, in the requested language and order. "),
                "merge_conditions": ("Preserve conditions and uncertainty. ", "Preserve uncertainty. "),
            }[arm]
            if original.count(before) != 1:
                raise ValueError("semantic wording differs from the declared baseline")
            guidance = original.replace(before, after)
            stack.enter_context(patch.object(answer_contract, "ANSWER_GUIDANCE", guidance))
            stack.enter_context(patch.object(answer_context, "ANSWER_GUIDANCE", guidance))
        if arm == "methods_short":
            from codey.research import methods

            original = methods.research_method_guidance

            def shorter_methods(*args, **kwargs):
                text = original(*args, **kwargs)
                return text.replace("; check workload and version", "").replace(
                    "\nCombine relevant methods; these suggestions are not an exhaustive domain classification.", "")

            stack.enter_context(patch.object(methods, "research_method_guidance", shorter_methods))
        yield


def worker(root, directory, base_url, case, seed, arm):
    if case == "legacy_methods":
        return legacy_worker(root, directory, base_url, case, seed, arm)
    with patch.object(branch, "ablation", treatment):
        branch.worker(root, directory, base_url, case, seed, arm)


def legacy_worker(root, directory, base_url, case, seed, arm):
    """The real legacy entry that consumes methods; no fabricated use in whole Research."""
    from urllib.request import urlopen

    from codey.providers.api_provider import ApiProvider
    from tests.test_research_deterministic_completion import _run

    branch.verify(root)
    directory.mkdir(parents=True, exist_ok=False)
    with urlopen(framework.UPSTREAM + "/v1/models", timeout=5) as response:
        model = json.load(response)["data"][0]["id"]
    fixture = next(row for row in framework.read_json(root / "fixtures.json")["cases"] if row["id"] == case)

    def record(row):
        with (directory / "research-wire.jsonl").open("a", encoding="utf8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    class Observed(ApiProvider):
        def _generate(self, messages, tools=None, **kwargs):
            record({"event": "generation_start", "messages": messages, "tools": [t.name for t in tools or ()]})
            result = super()._generate(messages, tools, **kwargs)
            record({"event": "generation_response", "body": result})
            return result

    provider = Observed(base_url + "/v1", model, native_tools=True, thinking_enabled=False,
                        output_tokens=2048, timeout=150, context_window_tokens=32768,
                        configure_request=lambda payload: record({"event": "admitted_payload", "payload": payload}))
    with treatment(arm), patch("codey.providers.request_context.clock_context",
                               return_value=(root / "clock-context.txt").read_text(encoding="utf8")):
        result = _run(directory, native=True, task=fixture["question"], source_text=fixture["documents"][0]["text"],
                      provider_factory=lambda _scripted: provider, budget=12)
    framework.write_json(directory / "research-result.json", {
        "stop_reason": result.stop_reason, "summary": result.summary, "synthesis_id": result.synthesis_id})
    (directory / "research-report.txt").write_text(result.summary, encoding="utf8")
    branch.verify(root)


def prepare(root: Path, previous: Path, group: str):
    root.mkdir(exist_ok=False)
    for name in ("clock-context.txt", "material-channel.json", "seed-identities.json", "fixtures.json"):
        shutil.copy2(previous / name, root / name)
    for name in ("empty-knowledge", "warm-knowledge", "inference-knowledge", "conflict-knowledge"):
        if (previous / name).exists():
            shutil.copytree(previous / name, root / name)
    choices = {
        "history": (["history", "history_error", "inference", "conflict"], ["baseline", "recall_header", "read_at_use"]),
        "long": (["long", "asyncio", "synthetic_conditions"], ["baseline", "next_read"]),
        "sentences": (["asyncio", "synthetic_conditions", "long"], ["baseline", "merge_task", "merge_conditions"]),
        "methods": (["legacy_methods"], ["baseline", "methods_short"]),
    }
    cases, arms = choices[group]
    fixtures = framework.read_json(root / "fixtures.json")
    if group == "methods":
        fixtures["cases"] = [{"id": "legacy_methods", "question": (
            "研究 SQLite 数据库的冻结测试材料，仅以 https://example.com/study 为来源。用中文回答："
            "1.读者和写者可以同时工作吗？2.同一时刻有几个写者？3.能否证明本机耗时更少？"
            "保留版本与条件，未测量的性能不要推断。"), "documents": [{
                "url": "https://example.com/study", "title": "SYNTHETIC database conditions",
                "text": "This is a synthetic database rule fixture, not an empirical benchmark. "
                        "For version X4 in WAL mode, readers and a writer may run concurrently. "
                        "Only one writer may write at a time to the same WAL file. "
                        "No elapsed time or machine workload was measured."}]}]
    fixtures["cases"] = [row for row in fixtures["cases"] if row["id"] in cases]
    if {row["id"] for row in fixtures["cases"]} != set(cases):
        raise ValueError("missing fixture")
    framework.write_json(root / "fixtures.json", fixtures)
    framework.write_json(root / "limits.json", {"turns": 18, "requests": 12, "process_seconds": 900, "agent_seconds": 840})
    framework.write_json(root / "preregistration.json", {
        "group": group, "phase": "A/B", "seeds": [661, 662],
        "matrix": [{"case": case, "arms": arms} for case in cases],
        "gate": ["Each candidate is assessed against the same case/seed baseline.",
                 "Body correctness, ordered reads and exact historical versions are separate from delivery.",
                 "No new pairwise semantic or publication regression in the intended scope; cost benefit or corrected failure.",
                 "Pre-existing shared failures do not veto a benefit in another scope.",
                 "Production adoption requires a fresh seed replication without experimental candidate patch."],
        "baseline": "Current production; every treatment is isolated in child processes.",
        "treatments": {
            "recall_header": "Only shorten compact recall header after the original row admission; identical exact rows.",
            "read_at_use": "Remove KNOWLEDGE_READ_GUIDANCE from the always-visible read schema; retain it in actual old-knowledge read results.",
            "next_read": "Add executable first-unread action only before EOF plus one concise unread-gap instruction; originals and guards unchanged.",
            "merge_task": "Merge only the first two task/language/order sentences.",
            "merge_conditions": "Remove only the second Preserve conditions phrase; preserve its uncertainty reminder.",
        },
        "limitations": "Exploratory fixed-source cases, two seeds. No claim about all domains, providers or global correctness.",
    })


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "freeze", "run", "worker"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--group", choices=("history", "long", "sentences", "methods"))
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--case")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--arm", choices=ARMS)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "prepare":
        prepare(root, args.previous.resolve(), args.group)
    elif args.action == "freeze":
        shared.freeze(root)
        print("FROZEN", root, flush=True)
    elif args.action == "worker":
        worker(root, args.directory, args.base_url, args.case, args.seed, args.arm)
    else:
        spec = framework.read_json(root / "preregistration.json")
        matrix = root / "matrix.json"
        rows = framework.read_json(matrix) if matrix.exists() else []
        branch.verify(root)
        with patch.object(shared, "__spec__", SimpleNamespace(name=__spec__.name)):
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
                        if any((row["case"], row["seed"], row["arm"]) == (item["case"], seed, arm) for row in rows):
                            continue
                        print("START", item["case"], seed, arm, flush=True)
                        row = shared.run_one(root, item["case"], seed, arm)
                        rows.append(row)
                        framework.write_json(matrix, rows)
                        print("END", item["case"], seed, arm, row.get("result"), flush=True)
                        framework.backend_ready()


if __name__ == "__main__":
    main()
