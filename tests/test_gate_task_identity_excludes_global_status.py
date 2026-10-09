"""Release gate task identity excludes global status events.

Repro: HeadlessAppContext.get_provider() emits a global
``type=status, status=connecting`` row without run/session ids. A normal
``run_headless()`` task returns ``done`` with rows
``task_start, status, turn, task_done``; the old
``check_single_session_identity()`` checked every row and returned
``ok=False`` for all normal agent cases (false negative).

Lock: only task-run events (task_start, turn, tool_started, tool,
task_done) participate; each carries non-empty consistent ids; at least
task_start and task_done must be present. Wording is "same Codey
run/session", never "same provider session" (ids cannot prove
new_chat() was not called).
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pytest


@pytest.mark.usefixtures("no_external_advisor_models")
class GateTaskIdentityExcludesGlobalStatusTests(unittest.TestCase):
    def test_global_status_without_ids_does_not_fail_identity(self) -> None:
        import tools.local_model_release_gate as gate

        rows = [
            {"type": "task_start", "run_id": "r1", "session_id": "s1"},
            {"type": "status", "status": "connecting"},
            {"type": "turn", "run_id": "r1", "session_id": "s1", "turn": 1},
            {"type": "task_done", "run_id": "r1", "session_id": "s1", "stop_reason": "done"},
        ]
        result = gate.check_single_session_identity(rows)
        self.assertTrue(result["ok"], f"global status must be excluded: {result}")

    def test_identity_requires_task_start_and_task_done(self) -> None:
        import tools.local_model_release_gate as gate

        only_tool = [
            {"type": "tool", "tool_name": "read_file", "run_id": "r1", "session_id": "s1"},
            {"type": "task_done", "run_id": "r1", "session_id": "s1", "stop_reason": "done"},
        ]
        self.assertFalse(
            gate.check_single_session_identity(only_tool)["ok"],
            "missing task_start must fail even with consistent ids",
        )
        only_start = [
            {"type": "task_start", "run_id": "r1", "session_id": "s1"},
            {"type": "tool", "tool_name": "read_file", "run_id": "r1", "session_id": "s1"},
        ]
        self.assertFalse(
            gate.check_single_session_identity(only_start)["ok"],
            "missing task_done must fail even with consistent ids",
        )

    def test_identity_requires_nonempty_consistent_ids_on_task_events(self) -> None:
        import tools.local_model_release_gate as gate

        split = [
            {"type": "task_start", "run_id": "r1", "session_id": "s1"},
            {"type": "tool", "tool_name": "read_file", "run_id": "r2", "session_id": "s1"},
            {"type": "task_done", "run_id": "r1", "session_id": "s1", "stop_reason": "done"},
        ]
        self.assertFalse(gate.check_single_session_identity(split)["ok"])
        missing = [
            {"type": "task_start", "run_id": "r1", "session_id": "s1"},
            {"type": "tool", "tool_name": "read_file", "run_id": "", "session_id": "s1"},
            {"type": "task_done", "run_id": "r1", "session_id": "s1", "stop_reason": "done"},
        ]
        self.assertFalse(gate.check_single_session_identity(missing)["ok"])

    @pytest.mark.usefixtures("scripted_local_api_connection")
    def test_real_headless_done_with_status_row_passes_identity(self) -> None:
        import tools.local_model_release_gate as gate
        from codey.app.headless_runner import HeadlessRequest, run_headless

        class _FakeDoneProvider:
            name = "fake-done"
            from codey.providers.token_accounting import ContextBudget
            context_budget = ContextBudget(32768, 8192, 0, 12000)

            def bind_usage(self, connection_id, sink):
                pass

            def new_chat(self, timeout=None) -> None:
                return None

            def send(self, prompt: str, timeout=None) -> str:
                del prompt, timeout
                return '{"tool": "done", "args": {"summary": "ok done"}}'

            def close(self) -> None:
                return None

        rows: list[dict] = []
        with tempfile.TemporaryDirectory() as td:
            result = run_headless(
                HeadlessRequest(
                    project=Path(td) / "proj",
                    task="do it",
                    provider_id="local",
                    max_turns=3,
                    intent="project",
                    project_changes_required=False,
                    state_home=Path(td) / "state",
                ),
                emit_jsonl=rows.append,
                connect_provider=lambda *args, **kwargs: _FakeDoneProvider(),
            )
        self.assertEqual(result.stop_reason, "done")
        kinds = [str(r.get("type") or "") for r in rows]
        # Real run preserves the auto-generated global status row.
        self.assertIn("status", kinds, f"expected auto status row, got {kinds}")
        self.assertIn("task_start", kinds)
        self.assertIn("task_done", kinds)
        identity = gate.check_single_session_identity(rows)
        self.assertTrue(identity["ok"], f"normal done must pass identity: {identity} rows={rows}")

    def test_docstring_does_not_claim_provider_session(self) -> None:
        import tools.local_model_release_gate as gate

        text = (gate.check_single_session_identity.__doc__ or "") + (gate.check_hybrid_tool_order.__doc__ or "")
        self.assertNotIn("provider session", text.lower())
        self.assertIn("codey", text.lower())
        self.assertIn("run", text.lower())


if __name__ == "__main__":
    unittest.main()
