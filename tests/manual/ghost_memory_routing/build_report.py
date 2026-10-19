"""Aggregate runs/*.jsonl into the A/B report."""
from __future__ import annotations

import json
from collections import defaultdict

from experiment_paths import DATA_ROOT

HERE = DATA_ROOT
RUNS_DIR = HERE / "runs"
ARMS = ("A", "B", "C")


def load() -> dict[str, list[dict]]:
    data: dict[str, list[dict]] = defaultdict(list)
    for arm in ARMS:
        path = RUNS_DIR / f"{arm}.jsonl"
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                data[arm].append(json.loads(line))
    return data


def stats(rows: list[dict]) -> dict:
    by_task = defaultdict(lambda: {"runs": 0, "passes": 0, "chars": []})
    for row in rows:
        cell = by_task[row["task"]]
        cell["runs"] += 1
        cell["passes"] += 1 if row.get("judge_passed") else 0
        cell["chars"].append(row.get("chars", 0))
    out = {}
    for task, cell in by_task.items():
        runs = cell["runs"]
        out[task] = {"passes": cell["passes"], "runs": runs,
                     "rate": round(cell["passes"] / runs, 3) if runs else None,
                     "chars_avg": round(sum(cell["chars"]) / runs, 1) if runs else None}
    done_runs = sum(1 for row in rows if row.get("stop_reason") == "done")
    return {"by_task": out,
            "done": done_runs,
            "total": len(rows),
            "stop_reasons": dict(_count(t.get("stop_reason") for t in rows))}


def _count(values) -> dict:
    out: dict[str, int] = defaultdict(int)
    for value in values:
        out[str(value or "")] += 1
    return dict(out)


def main() -> int:
    data = load()
    arms = {arm: stats(rows) for arm, rows in data.items()}
    report = {"arms": arms}
    decisive = ["t2_short", "t3_list", "t5_answer_first"]

    def passes(arm: str, task: str) -> int:
        cell = arms.get(arm, {}).get("by_task", {}).get(task)
        return cell["passes"] if cell else 0

    def runs(arm: str, task: str) -> int:
        cell = arms.get(arm, {}).get("by_task", {}).get(task)
        return cell["runs"] if cell else 0

    def total_rate(arm: str) -> tuple[int, int]:
        hits = sum(passes(arm, task) for task in decisive)
        denom = sum(runs(arm, task) for task in decisive)
        return hits, denom

    lines = ["# Ghost directive A/B report", ""]
    for arm in ARMS:
        rows = data.get(arm, [])
        hits, denom = total_rate(arm)
        lines.append(f"## Arm {arm}")
        lines.append(f"- runs: {arms.get(arm, {}).get('total', 0)} (done={arms.get(arm, {}).get('done', 0)})")
        lines.append(f"- decisive decisions: {hits}/{denom}")
        for task in sorted({row['task'] for row in rows}):
            cell = arms[arm]["by_task"].get(task, {})
            lines.append(f"  - {task}: {cell.get('passes')}/{cell.get('runs')} "
                         f"(chars_avg={cell.get('chars_avg')})")
        lines.append("")
    (HERE / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    (HERE / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
