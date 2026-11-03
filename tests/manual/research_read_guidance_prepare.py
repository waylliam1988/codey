"""Create isolated, explicit read-only fixtures before a historical-guidance trial."""

from __future__ import annotations

import argparse
import shutil
from copy import deepcopy
from pathlib import Path

from codey.knowledge.model import KnowledgeMutation, KnowledgeObject, KnowledgeReference
from codey.knowledge.store import KnowledgeStore
from tests.manual.research_framework_comparison import read_json, write_json
from tests.manual.research_read_guidance_ab import (
    LONG_LOCATOR,
    LONG_READ,
    LONG_RECALL,
    SHORT_LOCATOR,
    SHORT_READ,
    SHORT_RECALL,
)


def prepare(root: Path, previous: Path) -> None:
    root.mkdir(parents=True, exist_ok=False)
    for name in ("seed-identities.json", "clock-context.txt", "material-channel.json"):
        shutil.copy2(previous / name, root / name)
    for name in ("warm-knowledge", "empty-knowledge"):
        shutil.copytree(previous / name, root / name)
    ids = read_json(root / "seed-identities.json")
    old_cases = {row["id"]: row for row in read_json(previous / "fixtures.json")["cases"]}
    cases = [deepcopy(old_cases[name]) for name in ("history", "history_error")]
    inference_id, conflict_id = "inference-read-guidance-chain", "claim-read-guidance-conflict"
    original = KnowledgeReference(ids["claim_id"], 1, "depends_on")
    evidence = KnowledgeReference(ids["claim_value"]["references"][0]["target"], 1, "supported_by")
    synthetic = [
        ("inference", inference_id, "inference", "沿链核查", "model_inference",
         "沿链核查：根据旧主张，WAL 存在只读打开途径；这是否说明本机一定更快，尚未独立验证。", original),
        ("conflict", conflict_id, "claim", "冲突核查", "model_extraction",
         "冲突核查：SQLite 从 3.30.0 才支持只读 WAL，必须同时具备已有辅助文件、目录可写和 immutable 三项。", evidence),
    ]
    for case_id, identity, kind, alias, origin, content, ref in synthetic:
        folder = f"{case_id}-knowledge"
        shutil.copytree(root / "warm-knowledge", root / folder)
        store = KnowledgeStore(root / folder)
        try:
            store.repository.commit("fixture-read-guidance-" + case_id, [KnowledgeMutation(KnowledgeObject(
                identity, kind, content, origin, title=alias, aliases=(alias,),
                scope="personal", scope_id=store.owner_id, references=(ref,),
                provenance={"basis": "synthetic_read_only_fixture", "not_model_generated": True}))])
        finally:
            store.close()
        case = deepcopy(old_cases["history"])
        case.update(id=case_id, knowledge_seed=folder)
        if case_id == "inference":
            case["question"] = (
                "请从知识库找到‘沿链核查’旧推论，先读它，再沿 depends_on 读取旧主张及其当时引用的原始证据。"
                "用中文回答 SQLite 只读 WAL 的支持版本和完整 OR 条件，并判断这些证据能否证明本机一定更快。"
                "区分推论和原始证据，不用后来 source 头版本替代历史版本，不抓网页，不修改已有知识。")
        else:
            case["question"] = (
                "请从知识库找到‘冲突核查’候选主张，先读它，再沿其实际引用读取当时的原始证据，判断这条旧主张是否正确。"
                "用中文逐项核对 SQLite 只读 WAL 的版本、三个条件之间的关系、完整的各项条件，指出旧主张的错误。"
                "候选状态不代表已经验证，不用后来 source 头版本替代历史版本，不抓网页，不修改已有知识。")
        cases.append(case)
    ids["synthetic_fixture_ids"] = [inference_id, conflict_id]
    write_json(root / "seed-identities.json", ids)
    write_json(root / "fixtures.json", {"schema_version": 1, "cases": cases})
    write_json(root / "limits.json", {"turns": 18, "requests": 12, "process_seconds": 900, "agent_seconds": 840})
    write_json(root / "preregistration.json", {
        "scope": "Only shorten historical read guidance and compact recall header lines; preserve rows, tools and guards",
        "seeds": [81, 82, 83],
        "matrix": [{"case": case["id"], "arms": ["baseline", "compact"]} for case in cases],
        "wording": {"long": [LONG_READ, LONG_RECALL, LONG_LOCATOR],
                    "short": [SHORT_READ, SHORT_RECALL, SHORT_LOCATOR]},
        "controls": ["identical admitted recall rows/order/budget", "same tools and operation history",
                     "exact pinned ids/revisions", "same synthetic faults", "all failed runs included"],
        "quality_rubric": {
            "all": ["exactly one accepted saved answer with valid original evidence citations",
                    "Chinese answer: SQLite 3.22.0, not decoy head 9.99.0",
                    "OR between all three alternatives",
                    "both -shm and -wal exist AND readable",
                    "directory writable so both files can be created",
                    "immutable query parameter",
                    "actually follow original historical references; no live fetch or edits of old knowledge"],
            "history_error": ["recover from the explicit injected underscore-id read failure"],
            "inference": ["read named inference, then pinned premise and evidence",
                          "no claim of measured local speedup"],
            "conflict": ["read named candidate and original evidence; explicitly reject 3.30.0 and AND relation"],
        },
        "production_gate": ["no baseline-passing pair becomes a candidate failure",
                            "candidate fully passes at least 2/3 seeds in EACH case; both-fail cases are inconclusive",
                            "no identity/version/permission or forbidden edit/fetch regression",
                            "candidate aggregate requests do not increase",
                            "candidate aggregate input tokens fall >=1%; input+output also fall",
                            "input savings in at least 2/3 seed groups"],
        "phase2_if_pass": {"seeds": [91, 92], "arms": ["long_control", "production"],
                           "scope": "same four fixtures, unpatched production candidate; recheck every passing pair",
                           "gates": "no quality regression, no request increase; input and input+output both fall"},
        "limitations": "Exploratory four-scenario, three-seed read-only comparison, not a global capability claim",
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--previous", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.root.resolve(), args.previous.resolve())
