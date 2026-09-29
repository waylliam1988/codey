"""Hybrid gate requires successful tool evidence, not just names.

Repro: check_hybrid_tool_order() only looked at tool names. Rows with
``web_search``/``open_url`` ok=False still returned ok=True, so the gate
claimed "search and open sources" without proof.

Lock: every required hybrid step must carry ok=True; ``run`` additionally
requires a structured zero exit_code (text never implies pass).
"""
from __future__ import annotations

import unittest


def _hybrid_rows(*, search_ok=True, open_ok=True, run_exit=0, run_ok=True) -> list[dict]:
    return [
        {"type": "task_start", "run_id": "r1", "session_id": "s1"},
        {"type": "tool", "tool": "search", "tool_name": "web_search", "run_id": "r1", "session_id": "s1", "ok": search_ok},
        {"type": "tool", "tool": "read", "tool_name": "open_url", "run_id": "r1", "session_id": "s1", "ok": open_ok},
        {"type": "tool", "tool": "read_file", "tool_name": "read_file", "run_id": "r1", "session_id": "s1", "ok": True},
        {"type": "tool", "tool": "edit", "tool_name": "edit", "run_id": "r1", "session_id": "s1", "ok": True},
        {"type": "tool", "tool": "run", "tool_name": "run", "run_id": "r1", "session_id": "s1", "ok": run_ok, "exit_code": run_exit},
        {"type": "task_done", "run_id": "r1", "session_id": "s1", "stop_reason": "done"},
    ]


class HybridRequiresSuccessfulEvidenceTests(unittest.TestCase):
    def test_failed_search_does_not_pass_order(self) -> None:
        import tools.local_model_release_gate as gate

        order = gate.check_hybrid_tool_order(_hybrid_rows(search_ok=False))
        self.assertFalse(order["ok"], f"failed web_search must not pass: {order}")

    def test_failed_open_does_not_pass_order(self) -> None:
        import tools.local_model_release_gate as gate

        order = gate.check_hybrid_tool_order(_hybrid_rows(open_ok=False))
        self.assertFalse(order["ok"], f"failed open_url must not pass: {order}")

    def test_run_without_zero_exit_does_not_pass_order(self) -> None:
        import tools.local_model_release_gate as gate

        for bad in (1, None, "missing"):
            rows = _hybrid_rows(run_exit=bad)  # type: ignore[arg-type]
            order = gate.check_hybrid_tool_order(rows)
            self.assertFalse(order["ok"], f"run exit={bad!r} must not pass: {order}")

    def test_all_successful_steps_pass_order(self) -> None:
        import tools.local_model_release_gate as gate

        order = gate.check_hybrid_tool_order(_hybrid_rows())
        self.assertTrue(order["ok"], order)


if __name__ == "__main__":
    unittest.main()
