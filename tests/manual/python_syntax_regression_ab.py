"""Live A/B for a bounded Python syntax-regression hint.

The fault injection is probe-only and does not persist changes to Codey. Both
arms use the production Agent and edit implementation, then inject the same
deterministic syntax regression after the first successful target edit. The
baseline suppresses the production hint; the hint arm exposes it unchanged.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codey.agents import runner as agent
from codey.agents.request import AgentRequest
from codey.providers import controls as provider_controls
from codey.agents.tools import AgentToolFns
from codey.runtime.events import RunEvent, render_run_event
from codey.providers.registry import connect_provider, provider_ids
from tests.manual.project_task_context import render_production_project_map
from codey.toolchain.runtime import ToolOutcome, _python_syntax_regression_hint
from codey.toolchain.runtime import edit_file as runtime_edit_file


ARMS = ("baseline", "hint")


def run_agent(provider, project, task, **kwargs):
    return agent.run(AgentRequest(provider=provider, project=Path(project), task=task, **kwargs))
TARGET = "limits.py"
VALID_DEF = "def clamp(value):\n"
BROKEN_DEF = "def clamp(value)\n"
HINT_MARKER = "Syntax regression detected"
FILES = {
    TARGET: (
        "def clamp(value):\n"
        "    return min(10, max(0, value))\n"
    ),
    "test_limits.py": (
        "import unittest\n"
        "from limits import clamp\n\n"
        "class LimitTests(unittest.TestCase):\n"
        "    def test_upper_bound(self):\n"
        "        self.assertEqual(clamp(20), 8)\n\n"
        "if __name__ == '__main__':\n"
        "    unittest.main()\n"
    ),
}
TASK = (
    "Change only clamp's upper bound from 10 to 8. Preserve all other "
    "behavior. Run python -m unittest."
)


class _EditProbe:
    def __init__(self, root: Path, *, arm: str, inject_fault: bool) -> None:
        self.root = root
        self.arm = arm
        self.inject_fault = inject_fault
        self.original = runtime_edit_file
        self.injected = False
        self.generated_hints = 0
        self.exposed_hints = 0

    def __call__(self, root: Path, rel: str, blocks) -> ToolOutcome:
        path = self.root / rel
        before = path.read_text(encoding="utf-8") if path.is_file() else ""
        if self.arm == "baseline":
            with mock.patch(
                "codey.toolchain.runtime._python_syntax_regression_hint",
                return_value="",
            ):
                outcome = self.original(root, rel, blocks)
        else:
            outcome = self.original(root, rel, blocks)
        if not (outcome.ok and outcome.changed and rel == TARGET):
            return outcome

        after = path.read_text(encoding="utf-8")
        if self.inject_fault and not self.injected:
            if VALID_DEF not in after:
                raise AssertionError("probe could not inject the expected syntax regression")
            path.write_text(after.replace(VALID_DEF, BROKEN_DEF, 1), encoding="utf-8")
            after = path.read_text(encoding="utf-8")
            self.injected = True

        hint = _python_syntax_regression_hint(rel, before, after)
        if hint:
            self.generated_hints += 1
        if self.arm != "hint" or not hint:
            return outcome
        self.exposed_hints += 1
        return ToolOutcome(
            model_text=outcome.model_text + hint,
            ok=outcome.ok,
            exit_code=outcome.exit_code,
            changed=outcome.changed,
            truncated=outcome.truncated,
        )


def _write_project(root: Path) -> None:
    for rel, content in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _run_check(root: Path) -> bool:
    completed = subprocess.run(
        (sys.executable, "-B", "-m", "unittest"),
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )
    return completed.returncode == 0


def _run_arm(
    provider,
    *,
    arm: str,
    inject_fault: bool,
    max_turns: int,
) -> dict[str, Any]:
    events: list[RunEvent] = []
    with tempfile.TemporaryDirectory(prefix="codey-syntax-ab-") as td:
        root = Path(td)
        _write_project(root)
        probe = _EditProbe(root, arm=arm, inject_fault=inject_fault)
        result = run_agent(
            provider,
            root,
            TASK,
            max_turns=max_turns,
            on_event=events.append,
            fresh_chat=True,
            provider_id=getattr(provider, "id", ""),
            project_map=render_production_project_map(root, task=TASK),
            tool_fns=AgentToolFns(edit_file=probe),
        )

        final_content = (root / TARGET).read_text(encoding="utf-8")
        final_tests_passed = _run_check(root)
        try:
            ast.parse(final_content, filename=TARGET)
            final_syntax_valid = True
        except SyntaxError:
            final_syntax_valid = False

    tool_events = [event for event in events if event.kind == "tool" and event.call]
    run_events = [event for event in tool_events if event.call and event.call.name == "run"]
    report = {
        "arm": arm,
        "fault_injected": probe.injected,
        "stop_reason": result.stop_reason,
        "turns": result.turns,
        "tool_calls": len(tool_events),
        "reads": sum(1 for event in tool_events if event.call and event.call.name == "read"),
        "edits": sum(1 for event in tool_events if event.call and event.call.name == "edit"),
        "runs": len(run_events),
        "failed_runs": sum(
            1 for event in run_events if event.outcome is not None and not event.outcome.ok
        ),
        "generated_hints": probe.generated_hints,
        "exposed_hints": probe.exposed_hints,
        "final_syntax_valid": final_syntax_valid,
        "target_correct": "min(8, max(0, value))" in final_content,
        "final_tests_passed": final_tests_passed,
    }
    if result.stop_reason != "done" or not final_tests_passed:
        report["event_tail"] = [render_run_event(event) for event in events[-10:]]
    return report


def run_probe(
    provider_id: str,
    port: int,
    max_turns: int,
    order: str = "auto",
) -> dict[str, Any]:
    provider_controls.begin_task_context(f"python-syntax-regression-ab:{provider_id}")
    provider = None
    try:
        provider = connect_provider(provider_id, port=port)
        arms = ARMS
        if order == "hint-first" or (order == "auto" and provider_id in {"stepfun", "glm"}):
            arms = tuple(reversed(ARMS))
        fault_rows = {}
        for arm in arms:
            row = _run_arm(provider, arm=arm, inject_fault=True, max_turns=max_turns)
            fault_rows[arm] = row
            print(
                f"[fault {arm}] success={row['final_tests_passed']} "
                f"turns={row['turns']} tools={row['tool_calls']} "
                f"runs={row['runs']} reads={row['reads']} hints={row['exposed_hints']}",
                flush=True,
            )

        valid_control = _run_arm(
            provider,
            arm="hint",
            inject_fault=False,
            max_turns=max_turns,
        )
        print(
            f"[valid hint] success={valid_control['final_tests_passed']} "
            f"tools={valid_control['tool_calls']} hints={valid_control['exposed_hints']}",
            flush=True,
        )
    finally:
        try:
            if provider is not None:
                provider.close()
        finally:
            provider_controls.end_task_context()

    baseline = fault_rows["baseline"]
    hint = fault_rows["hint"]
    return {
        "provider": provider_id,
        "order": list(arms),
        "fault": {"baseline": baseline, "hint": hint},
        "valid_control": valid_control,
        "comparison": {
            "success_delta": int(hint["final_tests_passed"]) - int(baseline["final_tests_passed"]),
            "tool_call_delta": hint["tool_calls"] - baseline["tool_calls"],
            "run_delta": hint["runs"] - baseline["runs"],
            "read_delta": hint["reads"] - baseline["reads"],
            "legal_edit_false_positive": valid_control["exposed_hints"] > 0,
        },
    }


def run_self_test() -> None:
    valid_before = "def value():\n    return 1\n"
    valid_after = "def value():\n    return 2\n"
    invalid_after = "def value()\n    return 2\n"
    assert _python_syntax_regression_hint("app.py", valid_before, valid_after) == ""
    hint = _python_syntax_regression_hint("app.py", valid_before, invalid_after)
    assert HINT_MARKER in hint
    assert "line 1" in hint
    assert len(hint) < 400
    assert _python_syntax_regression_hint("app.js", valid_before, invalid_after) == ""
    assert _python_syntax_regression_hint("app.py", invalid_after, invalid_after) == ""
    print("self-test passed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live A/B for Python syntax regression hints.")
    parser.add_argument("--provider", choices=provider_ids(), default="stepfun")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument(
        "--order",
        choices=("auto", "baseline-first", "hint-first"),
        default="auto",
    )
    parser.add_argument("--out", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        run_self_test()
        return 0
    report = run_probe(args.provider, args.port, args.max_turns, args.order)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
