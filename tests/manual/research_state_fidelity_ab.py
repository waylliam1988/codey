"""Four independent, frozen presentation trials on the real Research entry.

No mutation normalization, reference adoption, automatic edits or relaxed gates.
Every candidate is confined to one presentation concern in isolated workers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.manual import research_branch_review as branch
from tests.manual import research_framework_comparison as framework
from tests.manual import research_guidance_simplification_ab as shared

CONDITION_RULE = (
    "Preserve every requested distinction, condition, exception and OR alternative. "
    "For a conditional rule, quote its short original condition clause beside your explanation; "
    "keep its AND/OR structure and implication direction, without adding a converse. "
)


def read_versions(session):
    versions = {}
    for result in session._memory_results.values():
        if not result.ok or result.call.name != "knowledge_read":
            continue
        try:
            value = json.loads(result.model_text)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and isinstance(value.get("id"), str) and type(value.get("revision")) is int:
            versions[value["id"], value["revision"]] = value
    return versions


def pending_references(session):
    """Pinned outgoing links of actually read prior work, without reading them."""
    read = read_versions(session)
    pending = {}
    for (identity, revision), value in read.items():
        if value.get("kind") not in {"claim", "inference", "observation", "question", "answer"}:
            continue
        for ref in value.get("references", []):
            target = (ref["target"], ref["revision"])
            if target not in read:
                row = pending.setdefault(target, {"tool": "knowledge_read", "args": {
                    "id": target[0], "revision": target[1]}, "linked_from": []})
                row["linked_from"].append({"id": identity, "revision": revision, "relation": ref["relation"]})
    return list(pending.values())


def edit_context(session):
    """Show exact existing full-value edits and the actual saved effects separately."""
    from tests.manual.research_branch_review import edit_template

    read = read_versions(session)
    committed = []
    saved = set()
    for result in session._memory_results.values():
        if not result.ok or result.call.name != "knowledge_commit":
            continue
        receipt = json.loads(result.model_text)
        committed.append({"receipt": receipt, "changes": result.call.args["changes"]})
        saved.update(receipt["versions"])
    templates = [edit_template(value)["edit_template_not_executed"]
                 for (identity, _), value in read.items()
                 if identity not in saved and value.get("kind") in {"claim", "inference", "observation"}
                 and value.get("content_complete") is True]
    return {"edit_templates_not_executed": templates, "already_saved_changes": committed}


def navigation(row):
    """Present coverage and exact read actions; do not decide relevance for the model."""
    if "available_length" not in row:
        return row
    args = ({"id": row["knowledge_source"][0], "revision": row["knowledge_source"][1]}
            if "knowledge_source" in row else {"url": row["url"]})
    tool = "knowledge_read" if "knowledge_source" in row else "open_url"
    intervals = row["read_intervals"]
    sections = []
    outline = row["sections"]
    for index, section in enumerate(outline):
        start = section["offset"]
        stop = outline[index + 1]["offset"] if index + 1 < len(outline) else row["available_length"]
        full = any(left <= start and stop <= right for left, right in intervals)
        partial = any(left < stop and start < right for left, right in intervals)
        sections.append({"label": section["label"], "status": "read" if full else "partial" if partial else "unread",
                         **({"read": {"tool": tool, "args": {**args, "offset": start}}} if not full else {})})
    return {key: value for key, value in row.items() if key not in {"source_id", "sections", "next_offset"}} | {
        "sections": sections,
        "next_read": ({"tool": tool, "args": {**args, "offset": row["next_offset"]}}
                      if row["next_offset"] < row["available_length"] else None)}


def rewrite_view(original, session, ledger, scope, visible, *, arm):
    from codey.knowledge.model import canonical_json

    text = original(session, ledger, scope, visible)
    if arm == "revision_state":
        text += ("\n\nStored knowledge changes (state, not evidence):\n" + canonical_json(edit_context(session))
                 + "\nAn already_saved change is complete: do not repeat it or create a replacement through "
                   "knowledge_updates. Continue investigating or submit the final answer. Templates keep the exact "
                   "read version; change only the requested value fields and choose a commit_id.")
    elif arm == "reference_actions":
        text += ("\n\nUnread pinned links from prior work (navigation only, not evidence):\n"
                 + canonical_json(pending_references(session))
                 + "\nFollow the links needed by the question until original source/evidence provides [eN] labels. "
                   "Prior conclusions and these navigation links are not citable originals.")
    elif arm == "reading_actions":
        blocks = text.split("\n\n")
        for index, block in enumerate(blocks):
            if not block.startswith('{"investigation_attempts":'):
                continue
            data = json.loads(block)
            data["source_locators"] = [navigation(row) for row in data["source_locators"]]
            blocks[index] = canonical_json(data)
        text = "\n\n".join(blocks)
        text += ("\n\nSection status comes from actual read ranges, not relevance. For requested points whose "
                 "sections are unread or partial, use their read action before answering. A section label is not evidence.")
    return text


@contextmanager
def treatment(arm):
    from codey.research import answer_context, answer_contract

    with ExitStack() as stack:
        if arm in {"revision_state", "reference_actions", "reading_actions"}:
            original = answer_context._render_context

            def view(*args):
                return rewrite_view(original, *args, arm=arm)

            stack.enter_context(patch.object(answer_context, "_render_context", view))
        elif arm == "condition_quotes":
            original = answer_contract.ANSWER_GUIDANCE
            guidance = original.replace(
                "Preserve every requested distinction, condition, exception and OR alternative. ", CONDITION_RULE)
            if guidance == original:
                raise ValueError("The exact baseline condition sentence changed.")
            stack.enter_context(patch.object(answer_contract, "ANSWER_GUIDANCE", guidance))
            stack.enter_context(patch.object(answer_context, "ANSWER_GUIDANCE", guidance))
        elif arm not in {"baseline", "production"}:
            raise ValueError(arm)
        yield


def worker(root, directory, base_url, case, seed, arm):
    with patch.object(branch, "ablation", treatment):
        branch.worker(root, directory, base_url, case, seed, arm)


def prepare(root):
    previous = Path(".e2e-artifacts/research-revision-longread-ab-v2-20261104")
    history = Path(".e2e-artifacts/research-history-prompts-ab-20261104")
    guidance = Path(".e2e-artifacts/research-guidance-production-20261104")
    root.mkdir(exist_ok=False)
    for name in ("clock-context.txt", "material-channel.json", "seed-identities.json"):
        shutil.copy2(previous / name, root / name)
    for folder in ("warm-knowledge", "empty-knowledge", "revision-knowledge", "conflict-knowledge"):
        shutil.copytree(previous / folder, root / folder)
    shutil.copytree(history / "inference-knowledge", root / "inference-knowledge")
    cases = {r["id"]: deepcopy(r) for r in framework.read_json(previous / "fixtures.json")["cases"]}
    history_cases = {r["id"]: deepcopy(r) for r in framework.read_json(history / "fixtures.json")["cases"]}
    guidance_cases = {r["id"]: deepcopy(r) for r in framework.read_json(guidance / "fixtures.json")["cases"]}
    content_root = Path(".e2e-artifacts/research-revision-content-ab-20261104")
    cases["revise_content"] = next(deepcopy(r) for r in framework.read_json(content_root / "fixtures.json")["cases"]
                                    if r["id"] == "revise_content")
    cases["inference"] = history_cases["inference"]
    cases["synthetic_conditions"] = guidance_cases["synthetic_conditions"]
    create_root = Path(".e2e-artifacts/research-revision-explicit-create-ab-20261104")
    cases["create_entity"] = next(deepcopy(r) for r in framework.read_json(create_root / "fixtures.json")["cases"]
                                  if r["id"] == "create_entity")
    overflow = deepcopy(cases["synthetic_conditions"])
    overflow["id"] = "overflow"
    original = overflow["documents"][0]["text"].splitlines()
    archive = "\n".join(f"Archive catalogue item {i:05}: historical filing label only, no policy findings." for i in range(1400))
    labels = ["C1 writer policy", "C2 writer policy", "Read-only alternatives", "Checkpoint progress",
              "Log reuse prerequisites", "Measurement boundaries"]
    body = original[0] + "\n\n## Archive catalogue\n" + archive + "\n\n" + "\n\n".join(
        "## " + label + "\n" + line for label, line in zip(labels, original[1:7], strict=True))
    overflow["documents"][0]["text"] = body
    cases["overflow"] = overflow
    selected = ["revise", "revise_content", "inference", "overflow", "long", "synthetic_conditions",
                "history", "accumulate", "create_entity"]
    framework.write_json(root / "fixtures.json", {"schema_version": 1, "cases": [cases[k] for k in selected]})
    framework.write_json(root / "limits.json", {"turns": 18, "requests": 10, "process_seconds": 780, "agent_seconds": 720})
    arms = {"revise": "revision_state", "revise_content": "revision_state", "inference": "reference_actions",
            "overflow": "reading_actions", "long": "condition_quotes", "synthetic_conditions": "condition_quotes"}
    framework.write_json(root / "preregistration.json", {
        "phase": "independent A/B; no production edits", "seeds": [721, 722],
        "matrix": [{"case": k, "arms": ["baseline", v]} for k, v in arms.items()],
        "treatments": {"revision_state": "Exact original full-value edit template and durable saved effect display only.",
                       "reference_actions": "Unread pinned reference actions, without automatic graph traversal/adoption.",
                       "reading_actions": "Per-section read status and exact existing tool actions.",
                       "condition_quotes": CONDITION_RULE},
        "gate": ["Count database edits, actual original reads, complete delivery and semantic accuracy separately.",
                 "Retain a local benefit only if baseline-passing pairs show no new failure or forbidden mutation.",
                 "New seeds and unpatched production confirmation required for adopted changes.",
                 "Budget failures, backend interruptions, preparation faults and all retries are retained separately."],
        "limitations": "Two exploratory seeds per fixture; controlled frozen material, not live Internet reliability.",
    })


def freeze(root):
    shared.freeze(root)
    hashes = framework.read_json(root / "frozen-inputs.json")
    for folder in ("revision-knowledge", "conflict-knowledge", "inference-knowledge"):
        hashes.update({p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in (root / folder).rglob("*") if p.is_file()})
    framework.write_json(root / "frozen-inputs.json", hashes)
    branch.verify(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "freeze", "run", "worker"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--case")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--arm")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "prepare":
        prepare(root)
    elif args.action == "freeze":
        freeze(root)
        print("FROZEN", root, flush=True)
    elif args.action == "worker":
        worker(root, args.directory, args.base_url, args.case, args.seed, args.arm)
    else:
        spec = framework.read_json(root / "preregistration.json")
        rows = framework.read_json(root / "matrix.json") if (root / "matrix.json").exists() else []
        with patch.object(shared, "__spec__", SimpleNamespace(name=__spec__.name)):
            for index, item in enumerate(spec["matrix"]):
                if args.case and args.case != item["case"]:
                    continue
                for seed in spec["seeds"]:
                    if args.seed is not None and args.seed != seed:
                        continue
                    arms = item["arms"]
                    shift = (seed + index) % len(arms)
                    for arm in arms[shift:] + arms[:shift]:
                        if args.arm and args.arm != arm:
                            continue
                        if any((r["case"], r["seed"], r["arm"]) == (item["case"], seed, arm) for r in rows):
                            continue
                        print("START", item["case"], seed, arm, flush=True)
                        row = shared.run_one(root, item["case"], seed, arm)
                        rows.append(row)
                        framework.write_json(root / "matrix.json", rows)
                        print("END", item["case"], seed, arm, row.get("result"), flush=True)
                        framework.backend_ready()


if __name__ == "__main__":
    main()
