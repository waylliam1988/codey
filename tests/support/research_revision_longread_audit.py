"""Portable side-effect checks for real Research revision/read experiments."""

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from tests.support import research_trial_audit as shared


def audit(root: Path):
    matrix = shared.read_json(root / "matrix.json", [])
    complete = []
    interrupted = []
    for trial in matrix:
        name = f"{trial['case']}-{trial['seed']}-{trial['arm']}"
        wire = shared.read_lines(root / "wire" / f"{name}.jsonl")
        payloads = [e for e in wire if e["event"] == "admitted_payload"]
        responses = [e for e in wire if e["event"] == "generation_response"]
        valid = len(payloads) == len(responses) and all(
            e.get("backend_perf", {}).get("last_seed") == trial["seed"] for e in responses)
        if valid:
            complete.append(trial)
        else:
            interrupted.append({"trial": trial, "submitted_requests": len(payloads), "completed_responses": len(responses),
                                "note": "Incomplete backend accounting; excluded from paired effectiveness/cost totals."})
    original = shared.read_json

    def read(path, default=None):
        return complete if path == root / "matrix.json" else original(path, default)

    with patch.object(shared, "read_json", read):
        rows = shared.audit(root)
    fixtures = {c["id"]: c for c in original(root / "fixtures.json")["cases"]}
    for row in rows:
        case = fixtures[row["case"]]
        if row["case"].startswith("long"):
            wire = shared.read_lines(root / "wire" / (row["name"] + ".jsonl"))
            texts = ["\n".join(str(m.get("content", "")) for m in e["payload"]["messages"])
                     for e in wire if e["event"] == "admitted_payload"]
            visibility = []
            for doc in case["documents"]:
                lines = [line for line in doc["text"].splitlines() if line.strip()]
                coverage = [sum(line in text for line in lines) for text in texts]
                visibility.append({"url": doc["url"], "source_characters": len(doc["text"]),
                                   "nonempty_source_lines": len(lines), "visible_lines_per_request": coverage,
                                   "whole_source_lines_visible_together": bool(lines) and len(lines) in coverage})
            row["source_visibility"] = visibility
        seed = case.get("knowledge_seed", "warm-knowledge" if case.get("warm") else "empty-knowledge")
        old = shared.knowledge_versions(root / seed)
        current = shared.knowledge_versions(root / "runs" / row["name"] / "knowledge")
        old_ids = {v["id"] for v in old}
        row["new_objects"] = [v for v in current if v["id"] not in old_ids]
        if row["case"] == "create_entity":
            created = [v for v in row["new_objects"] if v["kind"] == "entity"]
            row["entity_creation_check"] = {
                "created_entities": created,
                "requested_entity_once": len(created) == 1 and created[0]["id"] == "entity-revision-create-test"
                and created[0]["revision"] == 1 and created[0]["content"] == "SQLite is an embedded database engine."
                and created[0]["origin"] == "model_extraction",
                "existing_objects_unchanged": all(v in current for v in old)
                and all(max(v["revision"] for v in current if v["id"] == identity) == revision
                        for identity, revision in {v["id"]: v["revision"] for v in old}.items()),
            }
        alias = "冲突核查" if row["case"] == "revise_content" else "修订验证"
        targets = [v for v in old if v["kind"] == "claim" and alias in v["aliases"]]
        if row["case"].startswith("revise") and targets:
            before = max(targets, key=lambda v: v["revision"])
            versions = [v for v in current if v["id"] == before["id"]]
            added = [v for v in versions if v["revision"] > before["revision"]]
            expected = {k: v for k, v in before.items() if k not in {"revision", "commit_id"}}
            if row["case"] == "revise_content":
                expected["content"] = added[0]["content"] if added else before["content"]
            elif row["case"] == "revise_alias":
                expected["aliases"] = [*before["aliases"], "已核查只读条件"]
            else:
                expected["title"] = "只读 WAL 条件（核查修订）"
            exact = len(added) == 1 and added[0]["revision"] == before["revision"] + 1 and {
                k: v for k, v in added[0].items() if k not in {"revision", "commit_id"}} == expected
            if row["case"] == "revise_content":
                exact = exact and added[0]["content"] != before["content"]
            old_heads = {v["id"]: v["revision"] for v in old}
            heads = {v["id"]: v["revision"] for v in current}
            others_unchanged = all(heads.get(identity) == revision for identity, revision in old_heads.items()
                                   if identity != before["id"])
            row["revision_check"] = {
                "id": before["id"], "before": before, "added_versions": added,
                "exact_requested_edit_once": exact, "other_existing_heads_unchanged": others_unchanged,
                "no_replacement_claim": not row["new_claims"],
                "side_effect_pass": exact and others_unchanged and not row["new_claims"],
                "content_requires_semantic_review": row["case"] == "revise_content",
            }
        row["frozen_source"] = original(root / "experiment-source.json", {})
        row["question_sha256"] = hashlib.sha256(case["question"].encode()).hexdigest()
    result = {"root": root.name, "source": original(root / "experiment-source.json", {}),
              "preregistration": original(root / "preregistration.json", {}),
              "reviewed_trials": rows, "interrupted_trials": interrupted}
    (root / "revision-longread-audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
    return result


if __name__ == "__main__":
    import sys

    result = audit(Path(sys.argv[1]).resolve())
    for row in result["reviewed_trials"]:
        print(row["name"], row["stop_reason"], row["requests"], row["input_tokens"],
              row.get("revision_check", {}).get("side_effect_pass"))
    print("Interrupted:", len(result["interrupted_trials"]))
