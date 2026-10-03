"""Headless keeps missing canonical tool_name missing.

Repro: _payload_tool_started/_payload_tool filled
``tool_name`` from display ``kind`` when the canonical field was absent
(``tool_name = kind`` fallback). A spec-missing row then looked canonical
and the gate could accept it.

Lock: when the event has no canonical tool_name, the JSONL payload must
keep tool_name missing/empty so the gate rejects; display ``tool`` never
proves which tool ran.
"""
from __future__ import annotations

import unittest


class HeadlessMissingToolNameStaysMissingTests(unittest.TestCase):
    def test_tool_started_without_canonical_stays_missing(self) -> None:
        from codey.app.event_payloads import machine_event_payload

        payload = machine_event_payload(
            {"type": "tool_started", "run_id": "r1", "session_id": "s1", "kind": "read", "turn": 1}
        )
        assert payload is not None
        self.assertEqual(str(payload.get("tool") or ""), "read")
        self.assertFalse(str(payload.get("tool_name") or "").strip(), f"tool_name must stay missing: {payload}")

    def test_tool_without_canonical_stays_missing(self) -> None:
        from codey.app.event_payloads import machine_event_payload

        payload = machine_event_payload(
            {"type": "tool", "run_id": "r1", "session_id": "s1", "kind": "search", "turn": 1, "result": "x"}
        )
        assert payload is not None
        self.assertEqual(str(payload.get("tool") or ""), "search")
        self.assertFalse(str(payload.get("tool_name") or "").strip(), f"tool_name must stay missing: {payload}")

    def test_gate_rejects_rows_without_canonical_tool_name(self) -> None:
        import tools.local_model_release_gate as gate

        rows = [
            {"type": "task_start", "run_id": "r1", "session_id": "s1"},
            {"type": "tool", "tool": "search", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "read", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "read_file", "tool_name": "read_file", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "edit", "tool_name": "edit", "run_id": "r1", "session_id": "s1", "ok": True},
            {"type": "tool", "tool": "run", "tool_name": "run", "run_id": "r1", "session_id": "s1", "ok": True, "exit_code": 0},
            {"type": "task_done", "run_id": "r1", "session_id": "s1", "stop_reason": "done"},
        ]
        self.assertFalse(gate.check_hybrid_tool_order(rows)["ok"])


if __name__ == "__main__":
    unittest.main()
