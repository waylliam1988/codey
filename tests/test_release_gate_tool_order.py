"""Release gate must prove the hybrid tool order, not just files+done."""
from __future__ import annotations

import unittest


class ReleaseGateToolOrderTests(unittest.TestCase):
    def test_hybrid_task_prompt_requires_search_open_read_edit_verify_done(self) -> None:
        from tools import local_model_release_gate as gate

        task, intent, _ = gate._task_for("hybrid")
        lowered = task.lower()
        # Prompt must require the full chain the gate claims.
        for keyword in ("search", "read", "edit", "unittest", "done"):
            self.assertIn(keyword, lowered, f"hybrid prompt missing {keyword!r}: {task}")
        self.assertEqual(intent, "hybrid")

    def test_gate_helper_accepts_ordered_hybrid_rows(self) -> None:
        from tools import local_model_release_gate as gate

        rows = [
            {"type": "task_start", "run_id": "r1", "session_id": "s1"},
            {"type": "tool", "tool": "search", "tool_name": "web_search", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "read", "tool_name": "open_url", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "read_file", "tool_name": "read_file", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "edit", "tool_name": "edit", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "run", "tool_name": "run", "run_id": "r1", "session_id": "s1", "ok": True, "exit_code": 0},
            {"type": "task_done", "stop_reason": "done", "run_id": "r1", "session_id": "s1"},
        ]
        order = gate.check_hybrid_tool_order(rows)
        self.assertTrue(order["ok"], order)
        single = gate.check_single_session_identity(rows)
        self.assertTrue(single["ok"], single)

    def test_gate_helper_rejects_skipped_open(self) -> None:
        from tools import local_model_release_gate as gate

        rows = [
            {"type": "task_start", "run_id": "r1", "session_id": "s1"},
            {"type": "tool", "tool": "search", "tool_name": "web_search", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "read_file", "tool_name": "read_file", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "edit", "tool_name": "edit", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "run", "tool_name": "run", "run_id": "r1", "session_id": "s1", "ok": True, "exit_code": 0},
            {"type": "task_done", "stop_reason": "done", "run_id": "r1", "session_id": "s1"},
        ]
        order = gate.check_hybrid_tool_order(rows)
        self.assertFalse(order["ok"])
        self.assertIn("open", order["detail"])

    def test_gate_helper_rejects_split_session(self) -> None:
        from tools import local_model_release_gate as gate

        rows = [
            {"type": "task_start", "run_id": "r1", "session_id": "s1"},
            {"type": "tool", "tool": "search", "tool_name": "web_search", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "read", "tool_name": "open_url", "run_id": "r2", "session_id": "s1", "ok": True},
            {"type": "task_done", "run_id": "r1", "session_id": "s1", "stop_reason": "done"},
        ]
        single = gate.check_single_session_identity(rows)
        self.assertFalse(single["ok"])

    def test_gate_rejects_display_only_and_missing_ids(self) -> None:
        from tools import local_model_release_gate as gate

        display_only = [
            {"type": "task_start", "run_id": "r1", "session_id": "s1"},
            {"type": "tool", "tool": "search", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "read", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "read_file", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "edit", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "run", "run_id": "r1", "session_id": "s1", "ok": True, "exit_code": 0},
            {"type": "task_done", "run_id": "r1", "session_id": "s1", "stop_reason": "done"},
        ]
        self.assertFalse(gate.check_hybrid_tool_order(display_only)["ok"])
        missing = [
            {"type": "task_start", "run_id": "r1", "session_id": "s1"},
            {"type": "tool", "tool": "search", "tool_name": "web_search", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "read", "tool_name": "open_url", "run_id": "", "session_id": "", "ok": True},
            {"type": "tool", "tool": "read_file", "tool_name": "read_file", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "edit", "tool_name": "edit", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "run", "tool_name": "run", "run_id": "r1", "session_id": "s1", "ok": True, "exit_code": 0},
            {"type": "task_done", "run_id": "r1", "session_id": "s1", "stop_reason": "done"},
        ]
        self.assertFalse(gate.check_single_session_identity(missing)["ok"])
        self.assertFalse(gate.check_hybrid_tool_order(missing)["ok"])

    def test_full_projection_chain_passes_gate(self) -> None:
        import tools.local_model_release_gate as gate
        from codey.app.headless_runner import headless_event_payload
        from codey.operations import kernel_events as kev
        from codey.operations.task_session import TaskSession, turn_effect_id
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall, ToolResult
        from codey.runtime.observe.events import run_event_ui_payload

        policy = TaskPolicy(grants=frozenset({"control", "project.write"}))
        session = TaskSession(policy=policy, task_kind="hybrid", project="", max_turns=6)
        calls = [
            ToolCall(name="web_search", args={"query": "q"}),
            ToolCall(name="open_url", args={"url": "https://example.com/x"}),
            ToolCall(name="read_file", args={"path": "pricing.py"}),
            ToolCall(name="edit", args={"path": "pricing.py", "content": "x"}),
            ToolCall(name="run", args={"command": "python -m unittest discover", "path": "."}),
        ]
        results = []
        for c in calls:
            audit: dict = {"changed": (c.name == "edit")}
            if c.name == "run":
                audit["exit_code"] = 0
            results.append(ToolResult(call=c, model_text="ok", audit=audit))
        for idx, call in enumerate(calls):
            ident = turn_effect_id("r1:task", 1, idx)
            session.executed[ident] = {"name": call.name, "ok": True, "call_id": "", "excerpt": "ok", "args_digest": ""}
            session._memory_results[ident] = results[idx]
        session.record_verification(
            "python -m unittest discover", 1, True, exit_code=0,
            workspace_revision=1, workspace_fingerprint="sha256:" + "0" * 64,
        )
        events: list = []
        kev._emit_tool_results(events.append, session, results, run_id="r1:task", turn=1)
        rows: list[dict] = [{"type": "task_start", "run_id": "r1", "session_id": "s1"}]
        for ev in events:
            ui = run_event_ui_payload("r1", "s1", ev)
            self.assertIsNotNone(ui)
            payload = headless_event_payload(ui)  # type: ignore[arg-type]
            self.assertIsNotNone(payload)
            rows.append(payload)  # type: ignore[arg-type]
        rows.append({"type": "task_done", "run_id": "r1", "session_id": "s1", "stop_reason": "done"})
        self.assertTrue(gate.check_hybrid_tool_order(rows)["ok"], rows)
        self.assertTrue(gate.check_single_session_identity(rows)["ok"])

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
