"""Measure the same scripted operation cases before and after the refactor.

Run with ``python -m tests.manual.operation_execution_baseline --output PATH``.
Only the real kernel writes facts; this script supplies external observations.
Elapsed time is diagnostic, never a deterministic assertion.
"""
from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import tempfile
from pathlib import Path

from tests.support.operation_trace_probe import BAD, DONE, LENGTH, QUESTION, READ, SHELL, observe_trace


def measure(repeats: int) -> dict[str, object]:
    reports = {}
    cases = {
        "read_done": ([READ, DONE], {}),
        "protocol_repair": ([BAD, DONE], {}),
        "question": ([QUESTION], {}),
        "approval": ([SHELL], {"shell_approval": True}),
        "cancel_after_response": ([READ], {"cancel_after_reply": True}),
        "cancel_after_tool": ([READ], {"cancel_after_tool": True}),
        "no_progress": ([READ] * 6, {"max_turns": 6, "stagnant_turns": 2}),
    }
    for native in (False, True):
        for name, (steps, options) in cases.items():
            samples = []
            for _ in range(repeats):
                with tempfile.TemporaryDirectory(prefix="codey-operation-baseline-") as project:
                    samples.append(observe_trace(Path(project), steps, native=native, **options))
            elapsed = statistics.median(sample["metrics"]["elapsed_seconds"] for sample in samples)
            first = samples[0]
            metrics = {**first.pop("metrics"), "elapsed_seconds": elapsed}
            assert all({k: v for k, v in sample.items() if k != "metrics"} == first for sample in samples[1:])
            reports[f"{'native' if native else 'text'}_{name}"] = {**first, "metrics": metrics}
    for name, options in {
        "length_once": {}, "length_twice": {},
        "budget_close": {"max_turns": 1, "close_steps": 2},
        **{f"fault_{at}": {"interrupt_at": at} for at in (
            "response_returned", "tool_started", "before_post_tools", "after_post_tools")},
    }.items():
        steps = [LENGTH, DONE] if name == "length_once" else [LENGTH, LENGTH] if name == "length_twice" else [READ, DONE]
        with tempfile.TemporaryDirectory(prefix="codey-operation-baseline-") as project:
            reports[f"native_{name}"] = observe_trace(Path(project), steps, native=True, **options)
    return {"baseline_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "python": platform.python_version(), "repeats": repeats, "cases": reports}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".e2e-artifacts/operation-execution-candidate.json"))
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("repeats must be positive")
    report = measure(args.repeats)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Measured {len(report['cases'])} cases: {args.output}")


if __name__ == "__main__":
    main()
