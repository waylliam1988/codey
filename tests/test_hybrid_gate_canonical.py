"""Hybrid 门槛只认规范工具名：RunEvent -> UI 投影 -> headless -> gate 全链。

tool 字段是界面展示（search/read），tool_name 才是规范名
（web_search/open_url/read_file/edit/run）。门槛只读 tool_name，
且每个相关事件必须带一致的 run/session ID。
"""
from __future__ import annotations

import unittest


def _full_chain_rows(testcase, run_id="r1", session_id="s1"):
    from codey.app.headless_runner import headless_event_payload
    from codey.operations import kernel_events as kev
    from codey.operations.task_session import TaskSession
    from codey.operations.task_session import turn_effect_id as _tid
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall, ToolResult
    from codey.runtime.observe.events import run_event_ui_payload

    policy = TaskPolicy(grants=frozenset({"control", "project.write"}))
    session = TaskSession(policy=policy, task_kind="hybrid", project="", max_turns=6)
    calls = [
        ToolCall(name="web_search", args={"query": "pricing discount"}),
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
        results.append(ToolResult(ok=True, call=c, model_text="ok", audit=audit))
    for idx, call in enumerate(calls):
        ident = _tid("r1:task", 1, idx)
        session.executed[ident] = {
            "name": call.name, "ok": True, "call_id": "",
            "excerpt": "ok", "args_digest": "",
        }
        session._memory_results[ident] = results[idx]
    # Real verifications carry the observed workspace identity; the run
    # receipt below only counts with a structured zero exit.
    session.record_verification(
        "python -m unittest discover", 1, True, exit_code=0,
        workspace_revision=1, workspace_fingerprint="sha256:" + "0" * 64,
    )
    events: list = []
    kev._emit_tool_results(events.append, session, results, run_id="r1:task", turn=1)
    rows: list[dict] = [{"type": "task_start", "run_id": run_id, "session_id": session_id}]
    for ev in events:
        ui = run_event_ui_payload(run_id, session_id, ev)
        testcase.assertIsNotNone(ui)
        payload = headless_event_payload(ui)  # type: ignore[arg-type]
        testcase.assertIsNotNone(payload)
        rows.append(payload)  # type: ignore[arg-type]
    rows.append(
        {"type": "task_done", "run_id": run_id, "session_id": session_id, "stop_reason": "done"}
    )
    return rows


class HybridGateCanonicalTests(unittest.TestCase):
    def test_real_projection_chain_passes_hybrid_gate(self) -> None:
        import tools.local_model_release_gate as gate

        rows = _full_chain_rows(self)
        tool_names = [str(r.get("tool_name") or "") for r in rows if r.get("type") == "tool"]
        self.assertEqual(tool_names, ["web_search", "open_url", "read_file", "edit", "run"])
        displays = [str(r.get("tool") or "") for r in rows if r.get("type") == "tool"]
        self.assertIn("search", displays)
        order = gate.check_hybrid_tool_order(rows)
        self.assertTrue(order["ok"], f"real projection chain must pass gate: {order} rows={rows}")
        single = gate.check_single_session_identity(rows)
        self.assertTrue(single["ok"], single)

    def test_gate_requires_ids_on_every_relevant_row(self) -> None:
        import tools.local_model_release_gate as gate

        rows = [
            {"type": "task_start", "run_id": "r1", "session_id": "s1"},
            {"type": "tool", "tool": "search", "tool_name": "web_search", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "read", "tool_name": "open_url", "run_id": "", "session_id": "", "ok": True},
            {"type": "tool", "tool": "read_file", "tool_name": "read_file", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "edit", "tool_name": "edit", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "run", "tool_name": "run", "run_id": "r1", "session_id": "s1", "ok": True, "exit_code": 0},
            {"type": "task_done", "run_id": "r1", "session_id": "s1", "stop_reason": "done"},
        ]
        single = gate.check_single_session_identity(rows)
        self.assertFalse(single["ok"], f"missing ids must fail identity: {single}")
        order = gate.check_hybrid_tool_order(rows)
        self.assertFalse(order["ok"], f"missing ids must fail order: {order}")

    def test_gate_rejects_shuffled_and_display_only_rows(self) -> None:
        import tools.local_model_release_gate as gate

        rows = _full_chain_rows(self)
        # rows = [task_start, web_search, open, read, edit, run, task_done]
        shuffled = [rows[0], rows[3], rows[1], rows[2], rows[4], rows[5], rows[6]]
        order = gate.check_hybrid_tool_order(shuffled)
        self.assertFalse(order["ok"], f"shuffled must fail: {order}")
        display_only = [
            {"type": "task_start", "run_id": "r1", "session_id": "s1"},
            {"type": "tool", "tool": "search", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "read", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "read", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "edit", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "run", "run_id": "r1", "session_id": "s1", "ok": True, "exit_code": 0},
            {"type": "task_done", "run_id": "r1", "session_id": "s1", "stop_reason": "done"},
        ]
        order2 = gate.check_hybrid_tool_order(display_only)
        self.assertFalse(order2["ok"], f"display-only must fail: {order2}")


if __name__ == "__main__":
    unittest.main()
