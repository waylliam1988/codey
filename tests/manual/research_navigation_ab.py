"""Paired long-read display reduction; original tools and receipt recovery stay intact."""
from __future__ import annotations

import argparse
import hashlib
import json
import runpy
import secrets
import shutil
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from tests.manual import research_branch_review as branch
from tests.manual import research_framework_comparison as framework
from tests.manual import research_lazy_revision_ab as freezing
from tests.manual import research_read_guidance_ab as runner

ARMS = ("baseline", "compact", "original_control", "production")


def compact_view(text):
    """Delete only redundant display fields; preserve all other bytes and values."""
    from codey.knowledge.model import canonical_json

    parts = text.split("\n\n")
    changed = 0
    for index, part in enumerate(parts):
        stripped = part.lstrip()
        if not stripped.startswith('{"investigation_attempts":'):
            continue
        row = json.loads(stripped)
        row.pop("read_receipts")
        for locator in row["source_locators"]:
            locator.pop("unread_intervals", None)
            locator.pop("first_unread_interval", None)
        parts[index] = part[:len(part) - len(stripped)] + canonical_json(row)
        changed += 1
    if changed != 1:
        raise ValueError("Expected exactly one original work packet")
    return "\n\n".join(parts)


@contextmanager
def treatment(root, arm):
    from codey.research import answer_context

    if arm not in ARMS:
        raise ValueError(arm)
    original = answer_context.answer_context
    if arm == "compact":
        with patch.object(answer_context, "answer_context", lambda *a, **kw: compact_view(original(*a, **kw))):
            yield
    elif arm == "original_control":
        baseline = runpy.run_path(str(root / "baseline-answer-context.py"))["answer_context"]
        with patch.object(answer_context, "answer_context", baseline):
            yield
    else:
        yield


def document(identity, text):
    return {"url": "https://example.org/navigation/" + identity, "title": "SYNTHETIC navigation " + identity,
            "text": text}


def long_rule(identity, marker):
    intro = "## 1. Introduction\nThis is a synthetic navigation fixture, not a real product manual.\n\n"
    filler = ("Background measurement log. This line states no release, token or access rule.\n" * 180)
    rule = ("\n## 2. Access Rule\nRelease N7 uses exact token " + marker + ". This rule does not apply to N8. "
            "On N7, access is allowed if either both sidecar files already exist and are readable, "
            "or the parent directory is writable so both sidecars can be created. "
            "There is no immutable alternative in this synthetic fixture.\n\n")
    prefix = 4200 if identity == "gap" else 6500
    text = intro + filler[:prefix] + rule + "\n## 3. Background\n" + filler + "\n## 4. Closing\n" + filler[:1500]
    return document(identity, text), {"marker": marker, "start": text.index("Release N7"),
                                      "end": text.index("\n\n", text.index("Release N7"))}


def prepare(root, previous, phase, production=False):
    """Use fresh roots and retain every phase, including failures."""
    root.mkdir(parents=True, exist_ok=False)
    for name in ("clock-context.txt", "material-channel.json", "seed-identities.json"):
        shutil.copy2(previous / name, root / name)
    for name in ("empty-knowledge", "warm-knowledge"):
        shutil.copytree(previous / name, root / name)
    if production:
        for name in ("fixtures.json", "expectations.json", "baseline-answer-context.py"):
            shutil.copy2(previous / name, root / name)
        shutil.copytree(previous / "pinned-knowledge", root / "pinned-knowledge")
    else:
        cases, expected = [], {}
        for identity in ("continue", "gap", "pinned"):
            doc, values = long_rule(identity, "NAV-" + secrets.token_hex(5).upper())
            question = ("这是合成文档阅读测试。请继续阅读所给材料，用中文报告 Access Rule 的适用版本、"
                        "精确 token、完整的两个替代条件及 OR 关系，并说明是否适用于 N8、有无 immutable 替代。"
                        "必须引用实际读到的原文；没有读到规则时先继续读取，不用常识猜测。")
            sequence = [{"tool": "open_url", "args": {"url": doc["url"], "limit": 1000}}]
            if identity == "gap":
                sequence.append({"tool": "open_url", "args": {"url": doc["url"], "offset": 10000, "limit": 1000}})
            case = {"id": identity, "question": question, "documents": [doc], "controlled_sequence": sequence,
                    "controlled_reason": "Settle actual initial windows; all continuation decisions and publication are live."}
            if identity == "pinned":
                from codey.knowledge.model import KnowledgeMutation, KnowledgeObject
                from codey.knowledge.store import KnowledgeStore

                shutil.copytree(root / "empty-knowledge", root / "pinned-knowledge")
                store = KnowledgeStore(root / "pinned-knowledge")
                try:
                    store.repository.commit("navigation-seed", [KnowledgeMutation(KnowledgeObject(
                        "source-navigation-manual", "source", doc["text"], "source", title=doc["title"],
                        provenance={"url": doc["url"]}))])
                finally:
                    store.close()
                case.update(warm=True, knowledge_seed="pinned-knowledge",
                    question=question + "只能读取知识库 source-navigation-manual 的 revision=1；不能访问网页。",
                    controlled_sequence=[{"tool": "knowledge_read", "args": {
                        "id": "source-navigation-manual", "revision": 1, "limit": 1000}}])
            cases.append(case)
            expected[identity] = values
        old = framework.read_json(Path(".e2e-artifacts/research-branch-review-20261103-final/fixtures.json"))
        recovery = deepcopy(next(c for c in old["cases"] if c["id"] == "recovery"))
        token = "REC-" + secrets.token_hex(5).upper()
        recovery["documents"][0]["text"] = recovery["documents"][0]["text"].replace("ZEPHYR-6842-Q", token)
        cases.append(recovery)
        expected["recovery"] = {"marker": token, "requires_original_receipt": True}
        cases.append(deepcopy(next(c for c in old["cases"] if c["id"] == "long")))
        prior = framework.read_json(previous / "fixtures.json")["cases"]
        short = deepcopy(next(c for c in prior if c["id"] == "accumulate"))
        short["id"] = "short"
        cases.append(short)
        framework.write_json(root / "fixtures.json", {"schema_version": 1, "cases": cases})
        framework.write_json(root / "expectations.json", expected)
        shutil.copy2("codey/research/answer_context.py", root / "baseline-answer-context.py")
    seeds = [421, 422] if production else [401, 402]
    arms = ["original_control", "production"] if production else ["baseline", "compact"]
    framework.write_json(root / "limits.json", {"turns": 18, "requests": 10, "process_seconds": 900, "agent_seconds": 840})
    framework.write_json(root / "preregistration.json", {
        "phase": phase, "seeds": seeds, "matrix": [{"case": c["id"], "arms": arms} for c in framework.read_json(root / "fixtures.json")["cases"]],
        "change": "Remove read_receipts, unread_intervals and first_unread_interval from whole-answer display only. "
                  "Retain read_intervals, next_offset, source identities, sections and exact omitted-window recovery; all prose unchanged.",
        "gate": ["Paired target continuation/gap/pinned/recovery core task successes must not decrease; new candidate-only failures investigated.",
                 "No new stale/hidden citations, refetch violation, EOF next action or version/identity weakening.",
                 "Total actual input of target cases including retries must decrease, not just first requests.",
                 "Original SQLite long baseline may still be incomplete; compare all required facts and preserve failures, no universal perfection requirement.",
                 "Short original correctness and requested new knowledge saving must remain intact.",
                 "If integrated, repeat with new seeds and actual production versus frozen original control."],
        "limitations": "Exploratory two seeds; fixed synthetic documents and official SQLite snapshot; controlled prefixes explicitly separated from model decisions."})


def freeze(root):
    freezing.freeze(root)
    values = framework.read_json(root / "frozen-inputs.json")
    for name in ("baseline-answer-context.py", "expectations.json"):
        values[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    framework.write_json(root / "frozen-inputs.json", values)
    framework.write_json(root / "production-sha256.json", {
        p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path("codey").rglob("*.py"))})
    branch.verify(root)


def worker(root, directory, base_url, case, seed, arm):
    @contextmanager
    def apply(name):
        with treatment(root, name):
            yield

    with patch.object(branch, "ablation", apply):
        branch.worker(root, directory, base_url, case, seed, arm)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "freeze", "run", "worker"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--case")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--arm", choices=ARMS)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "prepare":
        prepare(root, args.previous.resolve(), "production" if args.production else "A/B", args.production)
        return
    if args.action == "freeze":
        freeze(root)
        return
    if args.action == "worker":
        worker(root, args.directory, args.base_url, args.case, args.seed, args.arm)
        return
    branch.verify(root)
    spec = framework.read_json(root / "preregistration.json")
    path = root / "matrix.json"
    rows = framework.read_json(path) if path.exists() else []
    for index, item in enumerate(spec["matrix"]):
        for seed in spec["seeds"]:
            arms = item["arms"]
            shift = (index + seed) % len(arms)
            for arm in arms[shift:] + arms[:shift]:
                if args.case and args.case != item["case"] or args.arm and args.arm != arm:
                    continue
                if any((r["case"], r["seed"], r["arm"]) == (item["case"], seed, arm) for r in rows):
                    continue
                print("START", item["case"], seed, arm, flush=True)
                with patch.object(runner, "__spec__", __spec__):
                    row = runner.run_one(root, item["case"], seed, arm)
                rows.append(row)
                framework.write_json(path, rows)
                print("END", item["case"], seed, arm, row.get("result"), flush=True)
                framework.backend_ready()


if __name__ == "__main__":
    main()
