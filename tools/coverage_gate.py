"""Require separately measured production statements and branches."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def check(report: dict, *, statements: float, branches: float) -> bool:
    if not report.get("meta", {}).get("branch_coverage") or not report.get("files"):
        raise ValueError("A nonempty production branch-coverage report is required")
    totals = report["totals"]
    if totals["num_statements"] <= 0 or totals["num_branches"] <= 0:
        raise ValueError("Statement and branch denominators must be positive")
    statement_rate = 100 * totals["covered_lines"] / totals["num_statements"]
    branch_rate = 100 * totals["covered_branches"] / totals["num_branches"]
    print(f"Production statements: {statement_rate:.2f}% (minimum {statements:.2f}%)")
    print(f"Production branches: {branch_rate:.2f}% (minimum {branches:.2f}%)")
    return statement_rate >= statements and branch_rate >= branches


def require_full_production_tree(report: dict, root: Path) -> None:
    measured = {name.replace("\\", "/") for name in report.get("files", {})}
    expected = {path.relative_to(root).as_posix() for path in (root / "codey").rglob("*.py")}
    if not expected or any(not name.startswith("codey/") for name in measured):
        raise ValueError("Coverage scope must be the production codey tree")
    missing = expected - measured
    if missing:
        raise ValueError(f"Production modules absent from coverage: {sorted(missing)[:8]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path(".e2e-artifacts/coverage/coverage.json"))
    parser.add_argument("--statements", type=float, default=85)
    parser.add_argument("--branches", type=float, default=75)
    args = parser.parse_args()
    if not (0 < args.statements <= 100 and 0 < args.branches <= 100):
        parser.error("Coverage minima must be in (0, 100]")
    report = json.loads(args.report.read_text(encoding="utf8"))
    require_full_production_tree(report, Path(__file__).resolve().parents[1])
    raise SystemExit(0 if check(report, statements=args.statements, branches=args.branches) else 1)


if __name__ == "__main__":
    main()
