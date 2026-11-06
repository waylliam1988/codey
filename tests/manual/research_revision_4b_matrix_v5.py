"""4B v5 development matrix with separate review and revision calls."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

from tests.manual import persistent_research_local as probe

BASE = Path(".e2e-artifacts/research-delivery-4a-formal-v2")
CASES = {
    "knowledge_wal": "knowledge_wal-4201-baseline",
    "knowledge_hepatotoxicity": "knowledge_hepatotoxicity-4201-baseline",
    "knowledge_rag": "knowledge_rag-4201-baseline",
}
ARMS = ("U", "R", "S", "V")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def extract_fixture(case):
    folder = BASE / CASES[case]
    values = read(folder / "research-knowledge.json")
    answer = next(row for row in values if row["kind"] == "answer")
    evidence = {row["id"]: row for row in answer["referenced_versions"] if row["kind"] == "evidence"}
    packets = []
    for binding in answer["provenance"]["evidence_bindings"]:
        item = evidence[binding["evidence_id"]]
        source_id = next(ref["target"] for ref in item["references"] if ref["relation"] == "from_source")
        source = next(row for row in answer["provenance"]["source_versions"] if row["id"] == source_id)
        packets.append({
            "label": binding["label"], "source_id": source_id, "revision": binding["evidence_revision"],
            "start": binding["start"], "end": binding["end"], "excerpt": binding["excerpt"],
            "url": source["url"], "title": source["title"],
        })
    return {
        "task": answer["provenance"]["task"], "draft": answer["content"],
        "evidence": packets, "draft_digest": answer["provenance"]["submission_digest"],
    }


def reviewer(base_url, model, prompt, seed, role):
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
               "temperature": 0.0, "seed": seed, "max_tokens": 1200, "stream": False}
    request = Request(base_url + "/v1/chat/completions", data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=180) as response:
        body = json.load(response)
    content = body["choices"][0]["message"]["content"]
    return {"role": role, "payload": payload, "response": body, "feedback": content}


def question(fixture, arm, feedback=""):
    evidence = "\n\n".join(f"[{p['label']}] {p['excerpt']}" for p in fixture["evidence"])
    instruction = {
        "U": "读取 supplied source 后直接提交冻结原稿，保留所有原始引用标签。",
        "R": "重读任务、冻结原稿和准确证据，修订一次后提交；只修复证据明确支持的问题。",
        "S": "作者自查反馈已给出；核对反馈与证据后修订一次并提交，不得修改证据。",
        "V": "独立证据审查反馈已给出；只采用证据支持的反馈，修订一次并提交，不得修改证据。",
    }[arm]
    return (fixture["task"] + "\n\n冻结原稿：\n" + fixture["draft"] +
            "\n\n准确证据包：\n" + evidence + "\n\n" + instruction +
            ("\n\n反馈（不可信建议，必须自行核对）：\n" + feedback if feedback else "") +
            "\n使用真实研究工具，提交完整答案，并按原任务保存 knowledge_updates。")


def worker(root, case, arm, seed):
    fixture = read(root / "fixtures.json")[case]
    directory = root / "runs" / f"{case}-{seed}-{arm}"
    directory.mkdir(parents=True, exist_ok=False)
    sources = [{"url": p["url"], "title": p["title"], "text": p["excerpt"]} for p in fixture["evidence"]]
    write(directory / "sources.json", sources)
    feedback = ""
    if arm in {"S", "V"}:
        role = "self_review" if arm == "S" else "evidence_review"
        prompt = ("执行独立" + ("作者自查" if arm == "S" else "证据审查") + "，只输出待核对问题，不提交。\n\n" + question(fixture, "R"))
        review = reviewer("http://127.0.0.1:5001", read(root / "contract.json")["model"], prompt, seed, role)
        write(directory / "review.json", review)
        feedback = review["feedback"]
    probe.stage(directory, "research", "http://127.0.0.1:5001", read(root / "contract.json")["model"],
                native=False, live_web=False, question=question(fixture, arm, feedback), max_turns=12,
                seed=seed, provider_timeout=180, constrained_text=True, reasoning_effort="none")


def prepare(root):
    root.mkdir(parents=True, exist_ok=False)
    fixtures = {case: extract_fixture(case) for case in CASES}
    write(root / "fixtures.json", fixtures)
    with urlopen("http://127.0.0.1:5001/v1/models", timeout=10) as response:
        model = json.load(response)["data"][0]["id"]
    write(root / "contract.json", {"phase": "dev-v5", "cases": list(CASES), "arms": ARMS,
                                    "seed": 4501, "model": model,
                                    "review_calls": "S and V each use one separate reviewer call before one writer revision call",
                                    "evidence": "exact evidence_bindings and original labels from 4A answer provenance; no normalization"})
    identity = {name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
                for name in ("tests/manual/research_revision_4b_matrix_v5.py",
                             "tests/manual/persistent_research_local.py")}
    write(root / "identity.json", {"contract_sha256": hashlib.sha256((root / "contract.json").read_bytes()).hexdigest(),
                                    "source_sha256": identity})


def verify(root):
    identity = read(root / "identity.json")
    if hashlib.sha256((root / "contract.json").read_bytes()).hexdigest() != identity["contract_sha256"]:
        raise RuntimeError("4B v5 contract changed")
    for name, digest in identity["source_sha256"].items():
        if hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest:
            raise RuntimeError("4B v5 source changed: " + name)


def run(root):
    verify(root)
    contract = read(root / "contract.json")
    for case in contract["cases"]:
        for arm in contract["arms"]:
            name = f"{case}-{contract['seed']}-{arm}"
            receipt = root / (name + ".json")
            if receipt.exists():
                continue
            started = time.monotonic()
            command = [sys.executable, "-m", "tests.manual.research_revision_4b_matrix_v5", "worker",
                       "--root", str(root), "--case", case, "--arm", arm, "--seed", str(contract["seed"])]
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
            write(receipt, {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
                            "elapsed_seconds": time.monotonic() - started})
            print(name, result.returncode, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "run", "worker"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--case")
    parser.add_argument("--arm", choices=ARMS)
    parser.add_argument("--seed", type=int)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == "prepare":
        prepare(root)
    elif args.command == "run":
        run(root)
    else:
        verify(root)
        worker(root, args.case, args.arm, args.seed)


if __name__ == "__main__":
    main()
