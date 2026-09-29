"""Release gate must prove the hybrid tool order, not just files+done."""
from __future__ import annotations

import unittest


def _tool_names(rows):
    names = []
    for row in rows:
        t = str(row.get("type") or "")
        if t in {"tool", "tool_start", "tool_result", "tool_event"}:
            name = str((row.get("call") or {}).get("name") or row.get("tool") or row.get("name") or "")
            if name:
                names.append(name.lower())
        # headless JSONL uses type=tool with call payload
        if t == "tool" and isinstance(row.get("call"), dict):
            pass
    return names


def assert_hybrid_order(names):
    """web_search -> open -> read -> edit -> run -> done (open/result/hit allowed)."""
    order = ["web_search", "open", "read_file", "edit", "run", "done"]

    def norm(n):
        if n in {"open_url", "open_result", "reopen_source", "open_hit"}:
            return "open"
        return n

    normed = [norm(n) for n in names]
    idx = 0
    for want in order:
        try:
            found = normed.index(want, idx)
        except ValueError as err:
            raise AssertionError(f"missing step {want!r} in order {normed}") from err
        idx = found + 1


class ReleaseGateToolOrderTests(unittest.TestCase):
    def test_hybrid_task_prompt_requires_search_open_read_edit_verify_done(self) -> None:
        from tools import local_model_release_gate as gate

        task, intent, _ = gate._task_for("hybrid")
        lowered = task.lower()
        # Prompt must require the full chain the gate claims.
        for keyword in ("search", "read", "edit", "unittest", "done"):
            self.assertIn(keyword, lowered, f"hybrid prompt missing {keyword!r}: {task}")
        self.assertEqual(intent, "hybrid")

    def test_hybrid_order_checker_accepts_good_trace(self) -> None:
        names = ["web_search", "open_url", "read_file", "edit", "run", "done"]
        assert_hybrid_order(names)

    def test_hybrid_order_checker_rejects_skipped_search(self) -> None:
        with self.assertRaises(AssertionError):
            assert_hybrid_order(["read_file", "edit", "run", "done"])

    def test_gate_helper_accepts_ordered_hybrid_rows(self) -> None:
        from tools import local_model_release_gate as gate

        rows = [
            {"type": "tool", "tool": "web_search", "run_id": "r1", "session_id": "s1"},
            {"type": "tool", "tool": "open_url", "run_id": "r1", "session_id": "s1"},
            {"type": "tool", "tool": "read_file", "run_id": "r1", "session_id": "s1"},
            {"type": "tool", "tool": "edit", "run_id": "r1", "session_id": "s1"},
            {"type": "tool", "tool": "run", "run_id": "r1", "session_id": "s1"},
            {"type": "task_done", "stop_reason": "done", "run_id": "r1", "session_id": "s1"},
        ]
        order = gate.check_hybrid_tool_order(rows)
        self.assertTrue(order["ok"], order)
        single = gate.check_single_session_identity(rows)
        self.assertTrue(single["ok"], single)

    def test_gate_helper_rejects_skipped_open(self) -> None:
        from tools import local_model_release_gate as gate

        rows = [
            {"type": "tool", "tool": "web_search", "run_id": "r1", "session_id": "s1"},
            {"type": "tool", "tool": "read_file", "run_id": "r1", "session_id": "s1"},
            {"type": "tool", "tool": "edit", "run_id": "r1", "session_id": "s1"},
            {"type": "tool", "tool": "run", "run_id": "r1", "session_id": "s1"},
            {"type": "task_done", "stop_reason": "done", "run_id": "r1", "session_id": "s1"},
        ]
        order = gate.check_hybrid_tool_order(rows)
        self.assertFalse(order["ok"])
        self.assertIn("open", order["detail"])

    def test_gate_helper_rejects_split_session(self) -> None:
        from tools import local_model_release_gate as gate

        rows = [
            {"type": "tool", "tool": "web_search", "run_id": "r1", "session_id": "s1"},
            {"type": "tool", "tool": "open_url", "run_id": "r2", "session_id": "s1"},
            {"type": "task_done", "stop_reason": "done", "run_id": "r1", "session_id": "s1"},
        ]
        single = gate.check_single_session_identity(rows)
        self.assertFalse(single["ok"])

    def test_run_agent_case_result_must_carry_tool_order(self) -> None:
        import inspect

        from tools import local_model_release_gate as gate

        source = inspect.getsource(gate.run_agent_case)
        # The gate currently only checks stop_reason/files/exit_code; it must
        # also assert tool events and single-session identity for hybrid.
        # This lock fails until the gate records and checks the order.
        self.assertIn("tool", source.lower())
        # Require explicit order assertion hook (checked in code, not just fixture).
        self.assertTrue(
            "order" in source.lower() or "sequence" in source.lower() or "tool_names" in source.lower(),
            "run_agent_case must assert hybrid tool order, not just files+done",
        )


if __name__ == "__main__":
    unittest.main()
