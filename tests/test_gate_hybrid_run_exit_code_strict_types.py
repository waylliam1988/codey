"""Hybrid gate run exit_code accepts only real int zero (bool/str rejected).

Repro (P2): _run_row_ok() did int(exit_code)==0, so int(False)==0 passed and
{"ok": True, "exit_code": False} was accepted as a successful run step.

Lock: False, True, "0", None, 1 all fail; only int 0 (and 0-compatible int
subclass-free values) pass. check_hybrid_tool_order() with a False exit_code
run row must be ok=False.
"""
from __future__ import annotations

import unittest

from tools import local_model_release_gate as gate


def _rows_with_run_exit(exit_value) -> list[dict]:
    base = {"run_id": "r1", "session_id": "s1"}
    return [
        {"type": "tool", "tool_name": "web_search", "ok": True, **base},
        {"type": "tool", "tool_name": "open_url", "ok": True, **base},
        {"type": "tool", "tool_name": "read_file", "ok": True, **base},
        {"type": "tool", "tool_name": "edit", "ok": True, **base},
        {"type": "tool", "tool_name": "run", "ok": True, "exit_code": exit_value, **base},
        {"type": "task_done", "stop_reason": "done", **base},
    ]


class HybridRunExitCodeStrictTypesTests(unittest.TestCase):
    def test_run_row_ok_rejects_bool_and_str(self) -> None:
        self.assertTrue(gate._run_row_ok({"ok": True, "exit_code": 0}))
        self.assertFalse(gate._run_row_ok({"ok": True, "exit_code": False}), "bool False must not be zero")
        self.assertFalse(gate._run_row_ok({"ok": True, "exit_code": True}))
        self.assertFalse(gate._run_row_ok({"ok": True, "exit_code": "0"}), "str '0' must not pass")
        self.assertFalse(gate._run_row_ok({"ok": True, "exit_code": None}))
        self.assertFalse(gate._run_row_ok({"ok": True, "exit_code": 1}))
        self.assertFalse(gate._run_row_ok({"ok": False, "exit_code": 0}))

    def test_hybrid_order_rejects_false_exit_code(self) -> None:
        out = gate.check_hybrid_tool_order(_rows_with_run_exit(False))
        self.assertFalse(out["ok"], f"False exit_code must fail hybrid order: {out}")
        out_ok = gate.check_hybrid_tool_order(_rows_with_run_exit(0))
        self.assertTrue(out_ok["ok"], f"int 0 must pass hybrid order: {out_ok}")

    def test_hybrid_order_rejects_string_exit_code(self) -> None:
        out = gate.check_hybrid_tool_order(_rows_with_run_exit("0"))
        self.assertFalse(out["ok"], f"string exit_code must fail hybrid order: {out}")


if __name__ == "__main__":
    unittest.main()
