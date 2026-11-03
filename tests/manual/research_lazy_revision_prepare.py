"""Preregister deferred revision tools before any live generation."""

from __future__ import annotations

import argparse
import shutil
from copy import deepcopy
from pathlib import Path

from codey.knowledge.model import KnowledgeMutation as M
from codey.knowledge.model import KnowledgeObject as K
from codey.knowledge.model import KnowledgeReference as R
from codey.knowledge.store import KnowledgeStore
from tests.manual.research_framework_comparison import read_json, write_json


def prepare(root, previous):
    root.mkdir(parents=True, exist_ok=False)
    for name in ("seed-identities.json", "clock-context.txt", "material-channel.json"):
        shutil.copy2(previous / name, root / name)
    for name in ("warm-knowledge", "empty-knowledge", "conflict-knowledge"):
        shutil.copytree(previous / name, root / name)
    old = {c["id"]: c for c in read_json(previous / "fixtures.json")["cases"]}
    cases = [deepcopy(old[c]) for c in ("history", "history_error", "conflict")]
    ids = read_json(root / "seed-identities.json")
    doc = old["history"]["documents"][0]
    shutil.copytree(root / "empty-knowledge", root / "revision-knowledge")
    store = KnowledgeStore(root / "revision-knowledge")
    common = {"scope": "personal", "scope_id": store.owner_id}
    source = K("source-wal-edit-test", "source", doc["text"], "source", title=doc["title"],
               status="active", provenance={"url": doc["url"], "basis": "explicit_synthetic_edit_fixture"}, **common)
    evidence = K("evidence-wal-edit-test", "evidence", doc["text"], "observed_result", title=doc["title"],
                 status="active", references=(R(source.id, 1, "from_source"),), **common)
    target = K("claim-wal-edit-test", "claim", "SQLite 自 3.22.0 起可按原文的 OR 条件读取只读 WAL。",
               "model_extraction", title="待核查：3.30.0 只读 WAL", aliases=("修订验证",),
               references=(R(evidence.id, 1, "supported_by"),), **common)
    try:
        store.repository.commit("fixture-lazy-edit", [M(source), M(evidence), M(target)])
        ids["revision_fixture"] = {"owner_id": store.owner_id, "source_id": source.id,
                                   "evidence_id": evidence.id, "target": target.to_dict(),
                                   "expected_title": "只读 WAL 条件（核查修订）"}
    finally:
        store.close()
    for name, instruction in (
        ("revise", "请找到并读取‘修订验证’已有候选 claim，把它的标题改为‘只读 WAL 条件（核查修订）’，"),
        ("conditional_revision", "请先读取‘修订验证’已有候选 claim，沿引用核查原文；如果标题含错误版本 3.30.0，"
         "就把这个已有对象的标题更正为‘只读 WAL 条件（核查修订）’，否则不要修改。"),
    ):
        case = deepcopy(old["history"])
        case.update(id=name, knowledge_seed="revision-knowledge", question=(instruction +
            "只改变标题，保留同一 ID、正文、状态、来源类型、条件、范围、provenance 和全部引用；"
            "提交为下一版本，不新建替代 claim，不重复提交新版本。"
            "随后用中文回答原始证据支持的只读 WAL 版本和完整 OR 条件。"
            "只说已修改不算完成，不抓网页。"))
        cases.append(case)
    cold = deepcopy(old["history"])
    cold.update(id="accumulate", warm=False)
    cold["question"] = ("阅读给定 SQLite 官方原文，用中文回答只读 WAL 的支持版本和完整 OR 条件；"
                        "同时把‘SQLite 从 3.22.0 开始放宽只读 WAL 限制’保存为一条新的候选 claim，"
                        "保留原始证据引用；不要把候选当成已经独立验证的知识。")
    cases.append(cold)
    write_json(root / "fixtures.json", {"schema_version": 1, "cases": cases})
    write_json(root / "seed-identities.json", ids)
    write_json(root / "limits.json", {"turns": 18, "requests": 12, "process_seconds": 900, "agent_seconds": 840})
    write_json(root / "preregistration.json", {
        "scope": "Only defer knowledge_commit behind an explicit no-write preparation tool; no task keyword classifier",
        "seeds": [101, 102, 103],
        "matrix": [{"case": c["id"], "arms": ["baseline", "lazy"]} for c in cases],
        "controls": ["original long historical guidance and exact recall rows", "all other schemas unchanged",
                     "original operation history, ids/revisions, permissions, publication and progress guards",
                     "no production edits during phase1; all failures and retries included"],
        "quality_rubric": {
            "all": ["one formal saved answer, real original evidence bindings; Chinese answer",
                    "3.22.0; full three OR alternatives: both files exist and readable OR writable directory to create both OR immutable",
                    "no live fetch except fixed given document in accumulate; no unauthorized old-object changes"],
            "history_error": ["one artificial underscore-id error, excluded from actual model call count"],
            "conflict": ["read candidate and original evidence; reject wrong 3.30.0 and AND relation; do not revise"],
            "revise_and_conditional_revision": ["read actual target before edit; exact same object with one new revision",
                                                "only title changes, all other fields and old history preserved",
                                                "successful original knowledge_commit, not just a claimed edit"],
            "accumulate": ["exactly one new candidate claim matching the requested source fact, via original publication"],
        },
        "production_gate": ["no baseline-passing paired task regresses; no forbidden writes/fetches or broken evidence bindings",
                            "baseline AND lazy each pass >=2/3 for every case, including real edit controls",
                            "lookup tasks omit preparation/commit and do not edit old objects",
                            "edit tasks reveal full unchanged commit schema after successful preparation",
                            "lookup aggregate input falls >=5%; input+output and overall input+output also fall",
                            "overall requests do not increase; lookup input savings in >=2/3 seed groups"],
        "phase2_if_pass": {"seeds": [111, 112], "arms": ["full_control", "production"],
                           "gates": "all tasks pass, no pair regresses, lookup input falls >=5%, overall input+output falls; no request increase"},
        "limitations": "Six scenarios on WAL materials, three seeds; exploratory, not statistical universal proof. Short-id edit fixtures explicitly simplify identity selection, not production.",
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--previous", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.root.resolve(), args.previous.resolve())
