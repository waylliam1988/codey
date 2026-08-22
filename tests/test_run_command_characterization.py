from __future__ import annotations

import unittest

from codey.events import RunEvent, run_event_ui_payload
from codey.models import ToolCall
from codey.tool_runtime import (
    RunCommandRawResult,
    ToolOutcome,
    project_run_command_result,
)


def _run_tool_event(outcome: ToolOutcome) -> RunEvent:
    return RunEvent.tool_finished(
        turn=2,
        call=ToolCall(name="run", args={"command": "pytest -q", "path": "."}),
        outcome=outcome,
    )


class RunCommandProjectionCharacterizationTests(unittest.TestCase):
    def test_model_text_and_audit_shape_without_timing(self) -> None:
        raw = RunCommandRawResult(
            command="pytest -q",
            output="1 passed",
            ok=True,
            exit_code=0,
        )
        outcome = project_run_command_result(root=None, raw=raw)

        self.assertEqual(outcome.model_text, "exit 0: pytest -q\n1 passed")
        self.assertTrue(outcome.ok)
        self.assertFalse(outcome.truncated)
        self.assertEqual(sorted(outcome.audit), ["exit_code"])
        self.assertEqual(outcome.audit.get("exit_code"), 0)

    def test_failure_exit_code_shape(self) -> None:
        raw = RunCommandRawResult(
            command="pytest -q",
            output="1 failed",
            ok=False,
            exit_code=1,
        )
        outcome = project_run_command_result(root=None, raw=raw)

        self.assertEqual(outcome.model_text, "exit 1: pytest -q\n1 failed")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.audit.get("exit_code"), 1)

    def test_timing_fields_are_audit_only(self) -> None:
        plain = project_run_command_result(
            root=None,
            raw=RunCommandRawResult(command="pytest -q", output="1 passed", ok=True, exit_code=0),
        )
        timed = project_run_command_result(
            root=None,
            raw=RunCommandRawResult(
                command="pytest -q",
                output="1 passed",
                ok=True,
                exit_code=0,
                started_at="2026-08-22T08:00:00.000Z",
                finished_at="2026-08-22T08:00:01.500Z",
                duration_ms=1500,
            ),
        )

        # Model-visible text must be byte-identical with and without timing.
        self.assertEqual(timed.model_text, plain.model_text)
        self.assertEqual(timed.presentation_status(), plain.presentation_status())
        self.assertEqual(
            sorted(timed.audit),
            [
                "command_duration_ms",
                "command_finished_at",
                "command_started_at",
                "exit_code",
            ],
        )
        self.assertEqual(timed.audit.get("command_started_at"), "2026-08-22T08:00:00.000Z")
        self.assertEqual(timed.audit.get("command_finished_at"), "2026-08-22T08:00:01.500Z")
        self.assertEqual(timed.audit.get("command_duration_ms"), 1500)


class RunEventUiPayloadCharacterizationTests(unittest.TestCase):
    def test_run_tool_payload_exact_shape_without_managed_output(self) -> None:
        outcome = ToolOutcome("exit 0: pytest -q\n1 passed", True, exit_code=0)
        payload = run_event_ui_payload("run-1", "session-1", _run_tool_event(outcome))

        self.assertEqual(payload, {
            "type": "tool",
            "run_id": "run-1",
            "session_id": "session-1",
            "turn": 2,
            "tool_id": "2:0",
            "kind": "run",
            "path": "",
            "result": "exit 0: pytest -q",
            "status": "ok",
            "error": False,
            "ok": True,
            "changed": False,
            "truncated": False,
            "command": "pytest -q",
            "exit_code": 0,
        })

    def test_run_tool_payload_exact_shape_with_managed_output(self) -> None:
        outcome = ToolOutcome(
            "exit 0: build\n[...]",
            True,
            exit_code=0,
            truncated=True,
            audit={
                "managed_output": {
                    "handle": "out_0001_abc123def456",
                    "original_bytes": 90000,
                    "stored_bytes": 40000,
                    "sha256": "a" * 64,
                },
            },
        )
        payload = run_event_ui_payload("run-1", "session-1", _run_tool_event(outcome))

        self.assertIsNotNone(payload)
        self.assertEqual(payload["type"], "tool")
        self.assertEqual(payload["output_handle"], "out_0001_abc123def456")
        self.assertEqual(payload["output_bytes"], 90000)
        self.assertEqual(payload["output_stored_bytes"], 40000)
        self.assertEqual(payload["output_sha256"], "a" * 64)
        self.assertNotIn("command_started_at", payload)
        self.assertNotIn("command_duration_ms", payload)


if __name__ == "__main__":
    unittest.main()
