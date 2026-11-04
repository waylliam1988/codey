"""Read live Research artifacts without changing or re-grading the tested run."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf8")) if path.exists() else default


def read_lines(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf8").splitlines()] if path.exists() else []


def knowledge_versions(root: Path):
    path = root / ".codey" / "knowledge.db"
    if not path.exists():
        return []
    with sqlite3.connect("file:" + path.as_posix() + "?mode=ro", uri=True) as database:
        return [{**json.loads(payload), "revision": revision, "commit_id": commit}
                for revision, payload, commit in database.execute(
                    "SELECT revision,payload,commit_id FROM knowledge_versions ORDER BY id,revision")]


def audit(root: Path):
    fixtures = {row["id"]: row for row in read_json(root / "fixtures.json")["cases"]}
    reviews = read_json(root / "body-review.json", {})
    rows = []
    for trial in read_json(root / "matrix.json", []):
        name = f"{trial['case']}-{trial['seed']}-{trial['arm']}"
        directory = root / "runs" / name
        case = fixtures[trial["case"]]
        seed = case.get("knowledge_seed", "warm-knowledge" if case.get("warm") else "empty-knowledge")
        old = {(row["id"], row["revision"]): row for row in knowledge_versions(root / seed)}
        versions = knowledge_versions(directory / "knowledge")
        current = {(row["id"], row["revision"]): row for row in versions}
        old_ids = {row[0] for row in old}
        answers = [row for row in versions if row["kind"] == "answer" and row["id"] not in old_ids]
        answer = answers[0] if len(answers) == 1 else None
        wire = read_lines(root / "wire" / f"{name}.jsonl")
        payloads = [row["payload"] for row in wire if row["event"] == "admitted_payload"]
        performance = [row["backend_perf"] for row in wire if row["event"] == "generation_response"]
        if len(performance) != len(payloads) or not all(row and row["last_seed"] == trial["seed"]
                                                                     for row in performance):
            raise ValueError("Incomplete backend accounting: " + name)
        calls, controlled = [], []
        for event in read_lines(directory / "research-wire.jsonl"):
            if event.get("event") != "generation_response":
                continue
            for choice in event.get("body", {}).get("choices", []):
                for call in choice.get("message", {}).get("tool_calls", []):
                    function = call["function"]
                    try:
                        arguments = json.loads(function["arguments"])
                    except ValueError:
                        arguments = {"unparseable": function["arguments"]}
                    (controlled if call["id"].startswith("controlled-") else calls).append({
                        "tool": function["name"], "args": arguments})
        body_path = directory / "research-report.txt"
        body = body_path.read_text(encoding="utf8") if body_path.exists() else ""
        provenance = answer["provenance"] if answer else {}
        legacy = "accepted_report" in provenance
        bindings = provenance.get("evidence_bindings", provenance.get("accepted_report", {}).get("evidence_bindings", []))
        exact = bool(bindings) and all(
            (row["source_id"], row["source_revision"]) in current
            and (row["evidence_id"], row["evidence_revision"]) in current
            and current[row["evidence_id"], row["evidence_revision"]]["content"] == row["excerpt"]
            and row["excerpt"] in current[row["source_id"], row["source_revision"]]["content"]
            for row in bindings)
        claims = [row for row in versions if row["kind"] == "claim" and row["id"] not in old_ids]
        same_batch = bool(answer) and all(
            row["status"] == "candidate" and row["commit_id"] == answer["commit_id"]
            and any(ref["relation"] == "supported_by" and (ref["target"], ref["revision"]) in {
                (binding["evidence_id"], binding["evidence_revision"]) for binding in bindings}
                    for ref in row["references"]) for row in claims)
        rows.append({"name": name, "case": trial["case"], "seed": trial["seed"], "arm": trial["arm"],
                     "stop_reason": trial.get("result", {}).get("stop_reason"),
                     "answer_mode": "legacy_finding_report" if legacy else "whole_answer",
                     "delivery": bool(answer) and trial.get("result", {}).get("stop_reason") == "done",
                     "body_matches_saved": bool(answer) and answer["content"].strip() == body.strip(),
                     "original_versions_unchanged": all(current.get(key) == value for key, value in old.items()),
                     "exact_original_references": exact, "requests": len(payloads),
                     "input_tokens": sum(row["last_input_count"] for row in performance),
                     "output_tokens": sum(row["last_token_count"] for row in performance),
                     "calls": calls, "controlled_calls": controlled, "body": body,
                     "body_sha256": hashlib.sha256(body.encode()).hexdigest(), "answer": answer,
                     "new_claims": claims, "claims_saved_with_answer": None if legacy else same_batch,
                     "body_review": reviews.get(name), "result": trial})
    (root / "audit.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf8")
    return rows


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    for row in audit(args.root.resolve()):
        print(row["name"], row["stop_reason"], row["requests"], row["input_tokens"])
