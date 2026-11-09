"""Complete the remaining browser experiments through existing gate scripts."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from codey.types.json_value import JsonObject, canonical_json_object


@dataclass(frozen=True)
class Experiment:
    name: str
    arguments: tuple[str, ...]
    calls: int
    diagnostic: bool = False


def plan() -> tuple[Experiment, ...]:
    observations = ("dynamic_expand", "policy_multistep", "control_static", "control_pdf")
    search = ("policy_ranked_search", "policy_followup_search", "research_conditions", "control_static")
    diagnostics = tuple(
        Experiment(f"projection-{projection or 'original'}-{repeat}",
                   ("--candidate", "--diagnostic", *(("--projection", projection) if projection else ())),
                   1, diagnostic=True)
        for repeat in range(1, 4) for projection in ("", "compact", "delta")
    )
    return (*diagnostics,
        Experiment("delta-v2", ("--projection", "delta", "--baseline-guidance", "browser_workflow",
                                "--cases", *observations), 24),
        Experiment("bounded-v1", ("--source-tools-only", "--projection", "bounded_read", "--cases",
                                  "research_long_exception", "control_captured", "control_static", "control_pdf"), 24),
        Experiment("settle-v1", ("--source-tools-only", "--projection", "settle_8s", "--cases",
                                 "policy_slow_hydration", "dynamic_expand", "control_static", "control_pdf"), 24),
        Experiment("query-v1", ("--source-tools-only", "--policy", "query", "--cases", *search), 24),
        Experiment("followup-v1", ("--source-tools-only", "--policy", "followup", "--cases", *search), 24),
        Experiment("ranking-v1", ("--source-tools-only", "--policy", "ranking", "--cases",
                                  "policy_ranked_search", "research_conflict", "control_static"), 18),
        Experiment("completion-v1", ("--source-tools-only", "--policy", "completion", "--cases",
                                     "research_expand_exception", "dynamic_select",
                                     "research_conditions", "control_restricted"), 24),
    )


def source_fingerprints(root: Path) -> JsonObject:
    """Freeze actual production bytes and all experiment dependencies."""
    production = hashlib.sha256()
    for path in sorted((root / "codey").rglob("*.py")):
        production.update(path.relative_to(root).as_posix().encode())
        production.update(path.read_bytes())
    experiment_paths = sorted((root / "tools").glob("browser_*.py"))
    return canonical_json_object({
        "production_python_sha256": production.hexdigest(),
        "runtime_sha256": hashlib.sha256((root / "codey/runtime.json").read_bytes()).hexdigest(),
        "experiment_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in experiment_paths},
    })


def run(directory: Path, *, timeout: float, selected_names: tuple[str, ...] = (), fixed_clock: bool = False) -> None:
    root = Path(__file__).resolve().parents[1]
    experiments = tuple(row for row in plan() if row.name in selected_names) if selected_names else plan()
    fingerprint = source_fingerprints(root)
    manifest: JsonObject = {
        **fingerprint,
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "calls": sum(row.calls for row in experiments),
        "timeout_seconds": timeout,
        "fixed_request_clock": fixed_clock,
        "experiments": [{"name": row.name, "arguments": list(row.arguments), "calls": row.calls,
                         "diagnostic": row.diagnostic} for row in experiments],
        "scope": "experiment_scripts_only_existing_headless_provider_kernel",
    }
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        if json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
            raise ValueError("suite inputs changed; use a new directory and retain prior artifacts")
    else:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    for experiment in experiments:
        target = directory / experiment.name
        if (target / "summary.json").is_file():
            continue
        if target.exists():
            raise ValueError(f"incomplete experiment retained at {target}; use a new suite directory")
        module = "tools.browser_semantic_live_gate" if experiment.diagnostic else "tools.browser_research_matrix"
        command = [sys.executable, "-X", "utf8", "-m", module, "--run-dir", str(target),
                   "--timeout", str(timeout), *experiment.arguments]
        if fixed_clock:
            command.append("--fixed-clock")
        print(f"START {experiment.name} ({experiment.calls} model runs)", flush=True)
        with (directory / f"{experiment.name}.log").open("w", encoding="utf-8") as log:
            result = subprocess.run(command, cwd=root, stdout=log, stderr=subprocess.STDOUT, check=False)
        if source_fingerprints(root) != fingerprint:
            raise RuntimeError("source bytes changed during frozen experiments")
        print(f"END {experiment.name}: exit={result.returncode}; artifacts={target}", flush=True)
        if not (target / "summary.json").is_file():
            raise RuntimeError(f"experiment did not produce a summary: {target}")
    (directory / "completed.json").write_text(json.dumps({"completed": [row.name for row in experiments],
        "production_unchanged": True, "limits": "Review actual branches and citation support before adoption."},
        indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--experiments", nargs="+", choices=[row.name for row in plan()], default=[])
    parser.add_argument("--fixed-clock", action="store_true")
    args = parser.parse_args()
    args.run_dir.mkdir(parents=True, exist_ok=True)
    run(args.run_dir.resolve(), timeout=args.timeout, selected_names=tuple(args.experiments), fixed_clock=args.fixed_clock)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
