"""KoboldCpp + Chromium gate for the production browser surface.

Registration, authorization, execution, CodeMode and receipts all remain in
the production entry. This file owns only the scenario matrix and provider
selection; it never registers tools, patches ANSWER_TOOLS, or installs an
alternate executor.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import local_model_gate_attempts as attempts
from tools.browser_production_live_gate import run_case
from tools.shell_web_diagnostic_gate import select_target

SCENARIOS = ("basic", "structured", "reread", "stale_edit", "upload", "download")


def _case(root: Path, scenario: str, target: attempts.GateTarget) -> dict[str, object]:
    options = {
        "restore_snapshots": scenario == "reread",
        "reread": scenario == "reread",
        "stale_edit": scenario == "stale_edit",
        "structured": scenario == "structured",
        "upload_file": scenario == "upload",
        "download_file": scenario == "download",
    }
    mode = "hybrid" if scenario == "stale_edit" else "chat"
    return run_case(root / scenario, mode, target, **options)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--scenario", choices=(*SCENARIOS, "all"), default="all")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--kobold-base-url", default="http://127.0.0.1:5001/v1")
    args = parser.parse_args()
    root = args.run_dir.resolve()
    root.mkdir(parents=True, exist_ok=False)
    target = select_target("gemma", args.timeout, args.kobold_base_url)
    selected = SCENARIOS if args.scenario == "all" else (args.scenario,)
    rows: list[dict[str, object]] = []
    for scenario in selected:
        print(json.dumps({"starting": scenario}), flush=True)
        row = _case(root, scenario, target)
        rows.append(row)
        print(json.dumps({"scenario": scenario, "passed": row.get("passed"),
                          "stop_reason": row.get("stop_reason"),
                          "successful_tools": row.get("successful_tools", [])}, ensure_ascii=False), flush=True)
    summary = {"passed": sum(row.get("passed") is True for row in rows), "runs": len(rows), "results": rows,
               "scope": "production_registry_kernel_codemode_receipts"}
    (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if summary["passed"] == summary["runs"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
