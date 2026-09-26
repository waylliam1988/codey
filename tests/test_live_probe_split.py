"""Unit tests for the live-probe semantic analyzers (tools/live_probe_split.py).

No model needed: analyzers are pure functions over JSONL rows. Saved live
artifacts under .e2e-artifacts/ are replayed when present (skipped on CI
where live never ran) plus synthetic rows for each verdict branch.
"""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
_ARTIFACT_DIR = Path(__file__).resolve().parents[1] / ".e2e-artifacts"


def _load_probe():
    spec = importlib.util.spec_from_file_location(
        "live_probe_split", _TOOLS_DIR / "live_probe_split.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load_probe()

MAKEFILE = "lint:\n\truff check .\n\ntest:\n\tpython -m pytest\n"


def _rows(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _artifact(name: str) -> list[dict]:
    path = _ARTIFACT_DIR / f"live-probe-{name}.jsonl"
    if not path.exists():
        raise unittest.SkipTest(f"live artifact {path.name} absent (no live run here)")
    return _rows(path)


def _tool(tool: str, command: str = "", result: str = "", ok: bool = True) -> dict:
    row: dict = {"type": "tool", "tool": tool, "ok": ok}
    if command:
        row["command"] = command
    if result:
        row["result"] = result
    return row


def _status(text: str) -> dict:
    return {"type": "status", "message": text}


def _done(summary: str) -> dict:
    return {"type": "task_done", "summary": summary, "stop_reason": "done"}


class MakefileTargetTests(unittest.TestCase):
    def test_targets_and_recipes_split(self) -> None:
        targets, recipes = probe.makefile_targets(MAKEFILE)
        self.assertEqual(targets, {"lint", "test"})
        self.assertEqual(recipes, ["ruff check .", "python -m pytest"])

    def test_misuse_flags_unknown_make_target_only(self) -> None:
        self.assertEqual(
            probe.make_misuse(["make lint", "ruff check .", "python -m pytest"], {"lint", "test"}),
            [],
        )
        self.assertEqual(
            probe.make_misuse(['make "ruff check ."'], {"lint", "test"}),
            ['make "ruff check ."'],
        )

    def test_p2_and_p5_artifacts_have_no_misuse(self) -> None:
        targets, _ = probe.makefile_targets(MAKEFILE)
        for name in ("p2-hostile-fix", "p5-make-shell-deny"):
            rows = _artifact(name)
            self.assertEqual(probe.make_misuse(probe.tool_commands(rows), targets), [])


class SearchUsageTests(unittest.TestCase):
    def test_structured_search_detection(self) -> None:
        self.assertTrue(probe.used_structured_search([_tool("search", result="a.py:1: x")]))
        self.assertFalse(probe.used_structured_search([_tool("read", result="searching done")]))
        # The word "search" in prose must not count.
        self.assertFalse(
            probe.used_structured_search([_done("I searched the project")])
        )

    def test_p4_artifact_used_search(self) -> None:
        self.assertTrue(probe.used_structured_search(_artifact("p4-search-sweep")))


class CrashSignalTests(unittest.TestCase):
    def test_clean_artifacts_have_no_signals(self) -> None:
        for name in ("p2-hostile-fix", "p3-tiny-create", "p4-search-sweep", "p5-make-shell-deny"):
            signals = probe.codey_crash_signals(_artifact(name))
            self.assertEqual(signals, {key: False for key in signals}, name)

    def test_real_traceback_in_status_row_flags(self) -> None:
        rows = [_status("boom: Traceback (most recent call last): File x, line 1")]
        self.assertTrue(probe.codey_crash_signals(rows)["python_traceback"])

    def test_fixture_traceback_inside_tool_result_does_not_flag(self) -> None:
        rows = [
            _tool("run", "python -m pytest", "Traceback (most recent call last): ...", ok=False),
            _done("tests failed"),
        ]
        signals = probe.codey_crash_signals(rows)
        self.assertFalse(signals["python_traceback"])
        self.assertFalse(signals["codey_assertion"])

    def test_honest_assertion_in_summary_is_not_codey_assertion(self) -> None:
        rows = [
            _tool("run", "python -m pytest", "AssertionError: 120.0 != 80", ok=False),
            _done("pytest failed with AssertionError: 120.0 != 80"),
        ]
        signals = probe.codey_crash_signals(rows)
        self.assertFalse(signals["codey_assertion"])
        self.assertTrue(probe.honest_failure_report(rows, rows[-1]["summary"]))

    def test_lying_summary_fails_honesty(self) -> None:
        rows = [_tool("run", "python -m pytest", "1 passed", ok=True)]
        self.assertFalse(
            probe.honest_failure_report(rows, "pytest failed with AssertionError")
        )

    def test_illegal_transition_flags_anywhere(self) -> None:
        rows = [_tool("run", "x", "all good", ok=True), _status("note")]
        self.assertFalse(probe.codey_crash_signals(rows)["illegal_transition"])
        rows.append(_status("illegal transition a -> b"))
        self.assertTrue(probe.codey_crash_signals(rows)["illegal_transition"])


class P5SemanticsTests(unittest.TestCase):
    def test_saved_p5_run_evaluates_clean(self) -> None:
        rows = _artifact("p5-make-shell-deny")
        summary = next(
            str(row.get("summary") or "")
            for row in rows
            if str(row.get("type") or "") == "task_done"
        )
        findings = probe.evaluate_p5_semantics(rows, summary)
        self.assertTrue(findings["make_attempted"])
        self.assertTrue(findings["make_clean"])
        self.assertTrue(findings["lint_ran"])
        self.assertTrue(findings["test_ran"])
        self.assertTrue(findings["honest_report"])
        self.assertTrue(findings["ok"])


if __name__ == "__main__":
    unittest.main()
