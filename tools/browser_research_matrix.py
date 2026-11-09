"""Run fixed paired cases through the production gate, preserving every failure."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from codey.types.json_value import JsonObject, canonical_json_object, decode_json_object
from tools.browser_observation_candidates import PROJECTIONS
from tools.browser_policy_cases import policy_cases
from tools.browser_policy_options import GUIDANCE
from tools.browser_research_cases import cases


def run(directory: Path, *, repeats: int, timeout: float, policy: str = "", projection: str = "",
        selected_names: tuple[str, ...] = (), baseline_guidance: str = "") -> JsonObject:
    available = (*cases(), *policy_cases())
    selected = tuple(case for case in available if case.name in selected_names) if selected_names else cases()
    case_path = Path(__file__).with_name("browser_research_cases.py")
    manifest: JsonObject = {"case_sha256": hashlib.sha256(case_path.read_bytes()).hexdigest(), "repeats": repeats,
                "gate_sha256": hashlib.sha256(Path(__file__).with_name("browser_semantic_live_gate.py").read_bytes()).hexdigest(),
                "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                "pair_order": "baseline/candidate alternates each repetition", "count": len(selected) * repeats * 2}
    if policy:
        manifest.update(policy=policy, policy_sha256=hashlib.sha256(
            Path(__file__).with_name("browser_policy_options.py").read_bytes()).hexdigest())
    if projection or selected_names or baseline_guidance:
        manifest.update(canonical_json_object({"projection": projection, "selected_names": list(selected_names),
            "baseline_guidance": baseline_guidance,
            "observation_sha256": hashlib.sha256(Path(__file__).with_name("browser_observation_candidates.py").read_bytes()).hexdigest(),
            "extra_cases_sha256": hashlib.sha256(Path(__file__).with_name("browser_policy_cases.py").read_bytes()).hexdigest()}))
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        if json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
            raise ValueError("matrix inputs changed; preserve this run and use a new directory")
    else:
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    rows: list[JsonObject] = []
    for repeat in range(1, repeats + 1):
        for case in selected:
            for candidate in ([False, True] if repeat % 2 else [True, False]):
                arm = "candidate" if candidate else "baseline"
                case_dir = directory / f"{case.name}-{repeat}-{arm}"
                if (case_dir / "summary.json").is_file():
                    report = decode_json_object(json.loads((case_dir / "summary.json").read_text(encoding="utf-8")))
                else:
                    command = [sys.executable, "-m", "tools.browser_semantic_live_gate", "--case", case.name,
                               "--run-dir", str(case_dir), "--timeout", str(timeout)]
                    if candidate or policy or projection or baseline_guidance:
                        command.append("--candidate")
                    if candidate and policy:
                        command.extend(("--guidance", policy))
                    elif baseline_guidance:
                        command.extend(("--guidance", baseline_guidance))
                    if candidate and projection:
                        command.extend(("--projection", projection))
                    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                               errors="replace", timeout=timeout * 16)
                    if not case_dir.exists():
                        case_dir.mkdir(parents=True)
                    (case_dir / "process.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
                    path = case_dir / "summary.json"
                    report = decode_json_object(json.loads(path.read_text(encoding="utf-8"))) if path.is_file() else {
                        "passed": False, "stop_reason": "gate_error", "process_exit_code": completed.returncode}
                    if not path.is_file():
                        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
                row = canonical_json_object({"case": case.name, "category": case.category, "repeat": repeat, "arm": arm,
                    "passed": report["passed"], "stop_reason": report["stop_reason"], "answer": report.get("answer", ""),
                    "usage": report.get("usage"), "seconds": report.get("seconds"), "directory": str(case_dir)})
                rows.append(row)
                (directory / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({key: value for key, value in row.items() if key not in {"answer", "usage"}}, ensure_ascii=False), flush=True)
    summary = canonical_json_object({**manifest, "completed": len(rows), "rows": rows,
        "limits": "Mechanical checks are screening; manual conclusion/citation review is required before default adoption."})
    (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--policy", choices=tuple(GUIDANCE), default="")
    parser.add_argument("--projection", choices=PROJECTIONS, default="")
    parser.add_argument("--baseline-guidance", choices=tuple(GUIDANCE), default="")
    parser.add_argument("--cases", nargs="+", choices=[case.name for case in (*cases(), *policy_cases())], default=[])
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    run(directory, repeats=args.repeats, timeout=args.timeout, policy=args.policy, projection=args.projection,
        selected_names=tuple(args.cases), baseline_guidance=args.baseline_guidance)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
