"""Red-first: live-probe PASS verdicts must not have false positives.

Covers review item 3: P4 must require used_search+exit0+done+hits;
P5 denied must not count as ran; --only empty/unknown must error.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"


def _load_probe():
    spec = importlib.util.spec_from_file_location(
        "local_model_diagnostic_probe_contract", _TOOLS_DIR / "local_model_diagnostic_probe.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


def _tool(tool: str, command: str = "", result: str = "", ok: bool = True) -> dict:
    row: dict = {"type": "tool", "tool": tool, "ok": ok}
    if command:
        row["command"] = command
    if result:
        row["result"] = result
    return row


def _done(summary: str, stop: str = "done") -> dict:
    return {"type": "task_done", "summary": summary, "stop_reason": stop, "turns": 3, "exit_code": 0}


class P4VerdictContractTests(unittest.TestCase):
    def test_no_search_cannot_pass(self) -> None:
        rows = [_done("found discount at pricing.py:1")]
        verdict = probe.evaluate_p4_semantics(rows, summary="found discount", exit_code=0, stop_reason="done")
        self.assertFalse(verdict["ok"])
        self.assertFalse(verdict["used_search"])

    def test_search_without_hits_or_done_cannot_pass(self) -> None:
        rows = [
            _tool("search", result="no hits"),
            {"type": "task_done", "summary": "nothing", "stop_reason": "stopped", "turns": 1, "exit_code": 1},
        ]
        verdict = probe.evaluate_p4_semantics(rows, summary="nothing", exit_code=1, stop_reason="stopped")
        self.assertFalse(verdict["ok"])

    def test_clean_search_with_hits_passes(self) -> None:
        rows = [
            _tool("search", result="pricing.py:1: discount\ntests/test_pricing.py:3: discount"),
            {"type": "task_done", "summary": "pricing.py:1 discount; tests/test_pricing.py:3 discount", "stop_reason": "done", "turns": 2, "exit_code": 0},
        ]
        verdict = probe.evaluate_p4_semantics(
            rows, summary="pricing.py:1 discount; tests/test_pricing.py:3 discount",
            exit_code=0, stop_reason="done",
            expected_hits={"pricing.py:1", "tests/test_pricing.py:3"},
        )
        self.assertTrue(verdict["ok"])


class P5VerdictContractTests(unittest.TestCase):
    def test_denied_commands_do_not_count_as_ran(self) -> None:
        rows = [
            _tool("run", "make lint", "denied", ok=False),
            _tool("run", "make test", "denied", ok=False),
            _done("done"),
        ]
        findings = probe.evaluate_p5_semantics(rows, "done")
        self.assertFalse(findings["lint_ran"])
        self.assertFalse(findings["test_ran"])
        self.assertFalse(findings["ok"])

    def test_real_ruff_and_pytest_results_required(self) -> None:
        rows = [
            _tool("run", "make lint", "ruff check .", ok=True),
            _tool("run", "ruff check .", "All checks passed!", ok=True),
            _tool("run", "make test", "pytest output", ok=False),
            _tool("run", "python -m pytest", "1 failed", ok=False),
            _done("ruff ok, pytest failed AssertionError: 120 != 80"),
        ]
        findings = probe.evaluate_p5_semantics(rows, rows[-1]["summary"])
        self.assertTrue(findings["lint_ran"])
        self.assertTrue(findings["test_ran"])

    def test_main_task_must_exit_zero_and_done(self) -> None:
        rows = [
            _tool("run", "ruff check .", "ok", ok=True),
            _tool("run", "python -m pytest", "ok", ok=True),
            {"type": "task_done", "summary": "ok", "stop_reason": "stopped", "turns": 1, "exit_code": 1},
        ]
        findings = probe.evaluate_p5_semantics(rows, "ok", exit_code=1, stop_reason="stopped")
        self.assertFalse(findings["ok"])


class ProbeCliContractTests(unittest.TestCase):
    def test_only_unknown_name_errors(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            probe.main(["--only", "typo"])
        self.assertNotEqual(ctx.exception.code, 0)

    def test_only_empty_selection_errors(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            probe.main(["--only", ""])
        self.assertNotEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
