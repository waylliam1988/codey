"""Run a sentence-level query-guidance ablation through the real Research gate.

This module is an experiment harness only.  It keeps production guidance intact,
uses the production source-tool set, and changes only the prompt addition passed
to the existing live gate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from codey.types.json_value import JsonObject, canonical_json_object, decode_json_object
from tools.browser_policy_options import QUERY_GUIDANCE_ABLATIONS

CASES = ("policy_ranked_search", "policy_followup_search")
VARIANTS = ("", "query", *QUERY_GUIDANCE_ABLATIONS)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_one(root: Path, directory: Path, case: str, guidance: str, *, timeout: float,
             fixed_clock: bool) -> JsonObject:
    command = [sys.executable, "-X", "utf8", "-m", "tools.browser_semantic_live_gate",
               "--case", case, "--run-dir", str(directory), "--timeout", str(timeout)]
    if guidance:
        command.extend(("--guidance", guidance))
    if fixed_clock:
        command.append("--fixed-clock")
    completed = subprocess.run(command, cwd=root, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout * 16)
    log = directory / "process.log"
    directory.mkdir(parents=True, exist_ok=True)
    log.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    summary_path = directory / "summary.json"
    if not summary_path.is_file():
        return canonical_json_object({"passed": False, "stop_reason": "gate_error",
                                      "process_exit_code": completed.returncode})
    return decode_json_object(json.loads(summary_path.read_text(encoding="utf-8")))


def run(directory: Path, *, repeats: int = 3, timeout: float = 180,
        fixed_clock: bool = True, variants: tuple[str, ...] = VARIANTS) -> JsonObject:
    """Run baseline, full guidance, and leave-one-sentence-out variants."""
    if repeats < 1:
        raise ValueError("repeats must be positive")
    root = Path(__file__).resolve().parents[1]
    manifest: JsonObject = canonical_json_object({
        "cases": list(CASES),
        "variants": list(variants),
        "repeats": repeats,
        "timeout_seconds": timeout,
        "fixed_request_clock": fixed_clock,
        "gate_sha256": _sha256(root / "tools/browser_semantic_live_gate.py"),
        "guidance_sha256": _sha256(root / "tools/browser_policy_options.py"),
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "scope": "real_model_production_source_tools_no_candidate_browser",
        "limits": "The result compares prompt variants; it does not attribute effects to production code.",
    })
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        previous = decode_json_object(json.loads(manifest_path.read_text(encoding="utf-8")))
        if previous != manifest:
            raise ValueError("ablation inputs changed; use a new run directory")
    else:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    rows: list[JsonObject] = []
    for repeat in range(1, repeats + 1):
        for case in CASES:
            for variant in variants:
                for arm in ("baseline", "candidate"):
                    arm_guidance = "" if arm == "baseline" else variant
                    label = variant or "none"
                    target = directory / f"{case}-{label}-{repeat}-{arm}"
                    summary_path = target / "summary.json"
                    if summary_path.is_file():
                        summary = decode_json_object(json.loads(summary_path.read_text(encoding="utf-8")))
                    else:
                        summary = _run_one(root, target, case, arm_guidance,
                                           timeout=timeout, fixed_clock=fixed_clock)
                    row = canonical_json_object({
                        "case": case,
                        "repeat": repeat,
                        "variant": variant or "none",
                        "arm": arm,
                        "guidance": arm_guidance,
                        "passed": summary.get("passed", False),
                        "stop_reason": summary.get("stop_reason", "unknown"),
                        "answer": summary.get("answer", ""),
                        "tools": summary.get("tools", []),
                        "seconds": summary.get("seconds"),
                        "directory": str(target),
                    })
                    rows.append(row)
                    (directory / "results.json").write_text(
                        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(json.dumps({key: value for key, value in row.items()
                                      if key not in {"answer"}}, ensure_ascii=False), flush=True)

    result = canonical_json_object({**manifest, "completed": len(rows), "rows": rows})
    (directory / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                             encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--no-fixed-clock", action="store_true")
    parser.add_argument("--variants", nargs="+", choices=VARIANTS, default=[])
    args = parser.parse_args()
    variants = tuple(args.variants) if args.variants else VARIANTS
    run(args.run_dir.resolve(), repeats=args.repeats, timeout=args.timeout,
        fixed_clock=not args.no_fixed_clock, variants=variants)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
