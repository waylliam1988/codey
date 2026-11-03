"""Run the modified production Research once per fixture/seed, without A/B."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from codey.knowledge.model import KnowledgeMutation, KnowledgeObject
from codey.knowledge.store import KnowledgeStore
from tests.manual import research_branch_review as branch
from tests.manual import research_framework_comparison as framework
from tests.manual import research_lazy_revision_ab as freezing
from tests.manual import research_no_revision_ab as runtime
from tests.manual import research_read_guidance_ab as runner


def clean_seed(source, target):
    """Rebuild actual revision-one objects; never reuse the synthetic corrupt head."""
    with sqlite3.connect("file:" + (source / ".codey/knowledge.db").resolve().as_posix() + "?mode=ro", uri=True) as db:
        owner = db.execute("SELECT value FROM knowledge_meta WHERE key='owner_id'").fetchone()[0]
        values = [KnowledgeObject.from_dict(json.loads(row[0])) for row in db.execute(
            "SELECT payload FROM knowledge_versions WHERE revision=1 ORDER BY id")]
    store = KnowledgeStore(target)
    try:
        with store.index.transaction() as db:
            db.execute("UPDATE knowledge_meta SET value=? WHERE key='owner_id'", (owner,))
        store.owner_id = owner
        if values:
            store.repository.commit("clean-production-smoke-seed", [KnowledgeMutation(value) for value in values])
        for value in values:
            if (value.kind == "source" and "content_hash" in value.provenance
                    and hashlib.sha256(value.content.encode()).hexdigest()[:16] != value.provenance["content_hash"]):
                raise ValueError("Seed source content does not match its identity hash")
            for ref in value.references:
                if store.repository.read(ref.target, ref.revision) is None:
                    raise ValueError("Seed reference is unavailable")
    finally:
        store.close()
    return {"owner": owner, "objects": len(values), "source_hashes_and_references_checked": True}


def prepare(root, previous):
    root.mkdir(parents=True, exist_ok=False)
    for name in ("seed-identities.json", "clock-context.txt", "material-channel.json"):
        shutil.copy2(previous / name, root / name)
    checks = {name: clean_seed(previous / name, root / name) for name in (
        "warm-knowledge", "empty-knowledge", "conflict-knowledge", "revision-knowledge")}
    cases = deepcopy(framework.read_json(previous / "fixtures.json")["cases"])
    conflict = deepcopy(next(case for case in cases if case["id"] == "conflict"))
    evidence = framework.read_json(root / "seed-identities.json")["claim_value"]["references"][0]["target"]
    conflict.update(id="after_read", controlled_sequence=[
        {"tool": "knowledge_read", "args": {"id": "claim-read-guidance-conflict", "revision": 1}},
        {"tool": "knowledge_read", "args": {"id": evidence, "revision": 1}}],
        controlled_reason="Settle real claim and evidence reads; all subsequent decisions and publication are live.")
    cases.append(conflict)
    framework.write_json(root / "fixtures.json", {"schema_version": 1, "cases": cases})
    framework.write_json(root / "limits.json", {"turns": 18, "requests": 10, "process_seconds": 900, "agent_seconds": 840})
    framework.write_json(root / "seed-validation.json", checks)
    framework.write_json(root / "preregistration.json", {
        "phase": "Production smoke only, no A/B requested by user", "seeds": [301, 302],
        "matrix": [{"case": case["id"], "arms": ["production"]} for case in cases],
        "scope": "Full revision schema from first turn; retain empty-search history dedup; preserve prior read statements",
        "review": ["Check actual accepted answer and database, not just submission receipt",
                   "Review version, all alternatives, logic direction and exact old-claim quotation",
                   "Check original source/evidence references and actual new candidate knowledge",
                   "Explicit revision outcome reported separately, no new object counted as old-object revision",
                   "after_read uses two controlled tool reads before live model continuation, explicitly reported"],
        "limitations": "Two seeds; frozen official excerpts through real read tools, not changing public web; no benefit comparison"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "freeze", "run", "worker"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--base-url")
    parser.add_argument("--case")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--arm", choices=("production",))
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "prepare":
        prepare(root, args.previous.resolve())
        return
    if args.action == "freeze":
        freezing.freeze(root)
        return
    if args.action == "worker":
        runtime.worker(root, args.directory, args.base_url, args.case, args.seed, "production")
        return
    branch.verify(root)
    spec = framework.read_json(root / "preregistration.json")
    path = root / "matrix.json"
    rows = framework.read_json(path) if path.exists() else []
    for item in spec["matrix"]:
        for seed in spec["seeds"]:
            if any((row["case"], row["seed"]) == (item["case"], seed) for row in rows):
                continue
            print("START", item["case"], seed, flush=True)
            with patch.object(runner, "__spec__", __spec__):
                result = runner.run_one(root, item["case"], seed, "production")
            rows.append(result)
            framework.write_json(path, rows)
            print("END", item["case"], seed, result.get("result"), flush=True)
            framework.backend_ready()


if __name__ == "__main__":
    main()
