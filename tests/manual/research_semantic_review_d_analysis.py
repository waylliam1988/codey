"""Independent post-run analysis; does not change frozen D requests or rerun models."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from pathlib import Path

from tests.manual.research_semantic_review_d import ARMS, ROOT, digest, write

OUTPUT = Path("docs/reports/research-semantic-review-d-analysis-20261106.json")


def corrected_score(row):
    identity = row["case"]
    answer = row["answer"]
    required = ["3.22.0", "-shm", "-wal", "writable", "immutable"] if identity in {"known_url", "two_errors", "source_discovery"} else []
    if identity in {"known_url", "two_errors"}:
        required.append("候选 Claim")
    if identity == "condition_scope":
        required = ["21", "28", "且", "或"]
    elif identity == "version_scope":
        required = ["V7", "V8", "9", "13", "5", "6"]
    elif identity == "error_complex":
        required = ["C1", "C2", "7", "11", "ARROW", "BIRCH", "INDEX-P"]
    forbidden = []
    if identity in {"known_url", "two_errors", "source_discovery"}:
        forbidden = ["key alone", "必须同时满足"]
    elif identity == "condition_scope":
        forbidden = [r"R4.*或.*audit", r"R5.*且"]
    elif identity == "version_scope":
        forbidden = [r"V7.*只读.*支持", r"V8.*不支持"]
    elif identity == "error_complex":
        forbidden = []
    labels = sorted(set(re.findall(r"\[(e\d+)\]", answer)))
    valid = set(labels) <= {"e1", "e2", "e3"}
    return {"required_hit": sum(term in answer for term in required), "required_total": len(required),
            "forbidden_count": sum(bool(re.search(pattern, answer, re.I)) for pattern in forbidden),
            "citation_labels": labels, "citation_labels_valid": valid, "chars": len(answer),
            "answer_digest": digest(answer)}


def main():
    paths = sorted((ROOT / "runs").glob("*.json"))
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    if len(rows) != 36:
        raise RuntimeError(f"expected 36 frozen runs, found {len(rows)}")
    for row in rows:
        row["corrected_score"] = corrected_score(row)
    groups = []
    for arm in ARMS:
        selected = [r for r in rows if r["arm"] == arm]
        groups.append({"arm": arm, "runs": len(selected),
            "required_hit": sum(r["corrected_score"]["required_hit"] for r in selected),
            "required_total": sum(r["corrected_score"]["required_total"] for r in selected),
            "forbidden_count": sum(r["corrected_score"]["forbidden_count"] for r in selected),
            "citation_invalid": sum(not r["corrected_score"]["citation_labels_valid"] for r in selected),
            "input_tokens": sum(r["input_tokens"] for r in selected), "output_tokens": sum(r["output_tokens"] for r in selected),
            "seconds": round(sum(r["seconds"] for r in selected), 3)})
    diffs = []
    for case in sorted({r["case"] for r in rows}):
        for seed in (941, 942):
            control = next(r for r in rows if r["case"] == case and r["seed"] == seed and r["arm"] == "control")
            for arm in ("self_check", "b_feedback"):
                candidate = next(r for r in rows if r["case"] == case and r["seed"] == seed and r["arm"] == arm)
                if candidate["answer"] != control["answer"]:
                    diff = list(difflib.unified_diff(control["answer"].splitlines(), candidate["answer"].splitlines(), lineterm=""))
                    diffs.append({"case": case, "seed": seed, "arm": arm,
                        "required_delta": candidate["corrected_score"]["required_hit"] - control["corrected_score"]["required_hit"],
                        "forbidden_delta": candidate["corrected_score"]["forbidden_count"] - control["corrected_score"]["forbidden_count"],
                        "diff": "\n".join(diff)[:12000]})
    result = {"basis": "frozen D runs; corrected deterministic rubric only; no model rerun",
              "groups": groups, "changed_answers": diffs,
              "production_unchanged": json.loads((ROOT / "preregistration.json").read_text(encoding="utf-8"))["identity"]["production"]
                  == {p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path("codey").rglob("*.py"))},
              "requests": len(rows), "model_calls": sum(len(r["calls"]) for r in rows),
              "warning": "Required-term and forbidden-pattern checks are a transparent completeness proxy, not a semantic proof."}
    write(OUTPUT, result)
    print(json.dumps(groups, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
