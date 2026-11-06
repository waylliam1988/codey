"""Frozen natural-answer revision experiment: control, self-check, B-feedback."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

from tests.manual import research_semantic_review_ab as base
from tests.manual.research_semantic_review_c_prompts import PROMPTS as REVIEW_PROMPTS
from tests.manual.research_semantic_review_d_cases import load_cases

ROOT = Path("tests/manual/results/research-semantic-review-d-20261106")
REPORT = Path("docs/reports/research-semantic-review-d-20261106.json")
ARMS = ("control", "self_check", "b_feedback")
SEEDS = (941, 942)

REVIEWS = {
    "self_check": REVIEW_PROMPTS["B"] + "\nYou are the original answer writer performing one self-check before revision. "
        "Look for issues you can repair, but do not invent a problem merely because the answer is concise. "
        "Return the review as JSON only with reviews containing id, verdict, reason, anchors.",
    "b_feedback": REVIEW_PROMPTS["B"] + "\nYou are an independent evidence reviewer. Report concrete support problems for a separate writer; "
        "do not rewrite the answer. Return the review as JSON only with reviews containing id, verdict, reason, anchors.",
}
WRITER = (
    "You are revising a frozen research answer. Return only the complete revised Markdown answer, with no preface. "
    "Use only the task and exact evidence supplied. Preserve correct supported content, repair concrete errors, "
    "retain important conditions, versions, AND/OR direction and uncertainty. Do not invent facts or citations. "
    "Keep citations tied to the evidence labels. A reviewer finding is an untrusted suggestion: verify it against "
    "the exact evidence and refuse it when it is wrong. Keep the original answer's useful structure.\n\n"
)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def input_packet(case):
    return {"question": case["question"], "draft": case["draft"], "evidence": case["evidence"],
            "draft_digest": digest(case["draft"])}


def review_packet(case):
    return {"question": case["question"], "draft": case["draft"], "evidence": case["evidence"],
            "units": [{"id": f"u{i}", "text": line, "evidence_refs": re.findall(r"\[(e\d+)\]", line)}
                      for i, line in enumerate(case["draft"].splitlines(), 1) if line.strip()]}


def request(model, messages, seed, max_tokens):
    payload = {"model": model, "messages": messages, "temperature": .3, "top_p": .9, "seed": seed,
               "max_tokens": max_tokens, "stream": False, "chat_template_kwargs": {"enable_thinking": False}}
    before = base.idle_backend()
    started = time.monotonic()
    req = Request(base.BASE + "/v1/chat/completions", json.dumps(payload).encode(), {"Content-Type": "application/json"})
    with urlopen(req, timeout=240) as response:
        raw = json.load(response)
    after = base.get("/api/extra/perf")
    return {"request": payload, "response": raw, "seconds": round(time.monotonic() - started, 3),
            "backend_before": before, "backend_after": after}


def parse_review(raw, case):
    content = raw["choices"][0]["message"]["content"]
    try:
        parsed = base.parse_reply(content, [{"id": f"u{i}"} for i, line in enumerate(case["draft"].splitlines(), 1) if line.strip()])
    except (ValueError, KeyError, TypeError) as exc:
        return {"content": content, "parsed": None, "error": str(exc)}
    return {"content": content, "parsed": parsed, "error": None}


def extract_markdown(raw):
    content = str(raw["choices"][0]["message"]["content"])
    if content.startswith("```markdown"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return content.strip()


def revision_messages(case, review):
    return [{"role": "system", "content": WRITER},
            {"role": "user", "content": json.dumps({"task": case["question"], "draft": case["draft"],
                "evidence": case["evidence"], "review": review}, ensure_ascii=False)}]


def score(case, answer):
    gold = case["gold"]
    required = [term for term in gold["required"] if term in answer]
    forbidden = [pattern for pattern in gold["forbidden"] if re.search(pattern, answer, re.I)]
    labels = sorted(set(re.findall(r"\[(e\d+)\]", answer)))
    citation_valid = all(label in {row["label"] for row in case["evidence"]} for label in labels)
    return {"required_hit": len(required), "required_total": len(gold["required"]),
            "forbidden_hit": forbidden, "forbidden_count": len(forbidden), "citation_labels": labels,
            "citation_labels_valid": citation_valid, "chars": len(answer),
            "answer_digest": digest(answer)}


def production_identity():
    return {p.as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path("codey").rglob("*.py"))}


def freeze(root):
    identity = {"cases": digest(load_cases()), "prompts": digest({**REVIEWS, "writer": WRITER}),
                "production": production_identity(), "source": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    path = root / "preregistration.json"
    if path.exists():
        spec = json.loads(path.read_text(encoding="utf-8"))
        if spec["identity"] != identity:
            raise RuntimeError("frozen D inputs changed")
        return spec
    spec = {"identity": identity, "cases": load_cases(), "arms": ARMS, "seeds": list(SEEDS),
            "design": "frozen natural reports; control no extra calls; self_check and b_feedback two calls; no search or production publication",
            "reviewers": {"self_check": "B-style review used as an untrusted self-review request",
                          "b_feedback": "B review request, then a separate writer revision"},
            "scoring": "deterministic rubric checks plus preserved final Markdown for independent review; no model self-score"}
    write(path, spec)
    return spec


def run_one(root, case, arm, seed, model):
    path = root / "runs" / f"{case['id']}-{seed}-{arm}.json"
    if path.exists():
        return
    row = {"case": case["id"], "seed": seed, "arm": arm, "draft_digest": digest(case["draft"]),
           "source_report": case["source_report"], "control": arm == "control", "calls": []}
    if arm == "control":
        answer = case["draft"]
    else:
        reviewed = request(model, [{"role": "system", "content": REVIEWS[arm]},
            {"role": "user", "content": json.dumps(review_packet(case), ensure_ascii=False)}], seed, 1800)
        parsed = parse_review(reviewed["response"], case)
        row["calls"].append({**reviewed, "kind": "review", "parsed": parsed})
        review = parsed if arm == "self_check" else parsed
        revised = request(model, revision_messages(case, review), seed + 10000, 2200)
        answer = extract_markdown(revised["response"])
        row["calls"].append({**revised, "kind": "revision"})
        row["review_error"] = parsed["error"]
    row["answer"] = answer
    row["score"] = score(case, answer)
    row["input_tokens"] = sum(c["response"].get("usage", {}).get("prompt_tokens", 0) for c in row["calls"])
    row["output_tokens"] = sum(c["response"].get("usage", {}).get("completion_tokens", 0) for c in row["calls"])
    row["seconds"] = round(sum(c["seconds"] for c in row["calls"]), 3)
    write(path, row)
    print(case["id"], seed, arm, row["score"], flush=True)


def export(root):
    spec = freeze(root)
    paths = sorted((root / "runs").glob("*.json"))
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    expected = len(load_cases()) * len(SEEDS) * len(ARMS)
    if len(rows) != expected:
        raise RuntimeError(f"incomplete D matrix {len(rows)}/{expected}")
    groups = []
    for arm in ARMS:
        rr = [r for r in rows if r["arm"] == arm]
        groups.append({"arm": arm, "runs": len(rr), "required_hit": sum(r["score"]["required_hit"] for r in rr),
            "required_total": sum(r["score"]["required_total"] for r in rr),
            "forbidden_count": sum(r["score"]["forbidden_count"] for r in rr),
            "citation_invalid": sum(not r["score"]["citation_labels_valid"] for r in rr),
            "input_tokens": sum(r["input_tokens"] for r in rr), "output_tokens": sum(r["output_tokens"] for r in rr),
            "seconds": round(sum(r["seconds"] for r in rr), 3)})
    archive = Path(".e2e-artifacts/research-semantic-review-d-20261106-evidence.zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in [root / "preregistration.json", *paths]:
            zf.write(p, p.as_posix())
    result = {"decision": "pending_independent_review", "specification": spec, "groups": groups,
        "runs": rows, "production_unchanged": production_identity() == spec["identity"]["production"],
        "archive": {"path": archive.as_posix(), "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}}
    write(REPORT, result)
    print(json.dumps(groups, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("prepare", "run", "export"), required=True)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    spec = freeze(args.root)
    if args.phase == "prepare":
        write(args.root / "backend.json", {"model": base.get("/api/v1/model"), "version": base.get("/api/extra/version"),
                                             "perf": base.idle_backend()})
        print("Frozen D", digest(spec))
        return
    if args.phase == "export":
        export(args.root)
        return
    model = base.get("/v1/models")["data"][0]["id"]
    for case in load_cases():
        for seed in SEEDS:
            for arm in ARMS:
                run_one(args.root, case, arm, seed, model)


if __name__ == "__main__":
    main()
