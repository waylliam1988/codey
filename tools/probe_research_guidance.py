"""Phase-controlled guidance probes using the existing real-model experiment gate."""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import replace
from pathlib import Path

from tools.browser_policy_cases import policy_cases
from tools.browser_remaining_experiments import source_fingerprints
from tools.browser_research_cases import ROOT, BrowserResearchCase
from tools.browser_semantic_live_gate import run


def probe_case(mechanism: str) -> BrowserResearchCase:
    name = "policy_followup_search" if mechanism == "followup" else "policy_ranked_search"
    original = next(case for case in policy_cases() if case.name == name)
    if mechanism == "followup":
        task = (original.task + f" 请从已知的一般规则原文 {ROOT}retention 开始阅读，"
                "再核对这份原文没有覆盖的例外。不要保存知识。")
    else:
        task = "先使用网页搜索获得检索线索，不要猜测URL；再选择原始资料核对。" + original.task
    return replace(original, task=task)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--mechanism", choices=("followup", "ranking"), required=True)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    directory = args.run_dir.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    fingerprint = source_fingerprints(root)
    case = probe_case(args.mechanism)
    (directory / "probe-manifest.json").write_text(json.dumps({
        **fingerprint, "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "mechanism": args.mechanism, "task": case.task, "count": 6,
        "limits": "Starting phase is controlled; this cannot replace autonomous natural-task results.",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    for repeat in range(1, 4):
        for candidate in ([False, True] if repeat % 2 else [True, False]):
            arm = "candidate" if candidate else "baseline"
            target = directory / f"{args.mechanism}-{repeat}-{arm}"
            target.mkdir()
            report = run(target, args.timeout, candidate=False, diagnostic=False, case=case,
                         guidance=args.mechanism if candidate else "", fixed_clock=True)
            if source_fingerprints(root) != fingerprint:
                raise RuntimeError("source changed during phase-controlled probes")
            print(json.dumps({"repeat": repeat, "arm": arm, "passed": report["passed"],
                              "stop_reason": report["stop_reason"]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
