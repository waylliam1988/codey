"""Validator exceptions must reject tool calls (fail closed).

Locks: ``validate_args_against_spec`` raising must produce a protocol
error for both JSON and native turns, never an accepted ``done``/tool call.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock


def _policy():
    return SimpleNamespace(allows=lambda grant: True)


class ValidatorExceptionFailClosedTests(unittest.TestCase):
    def test_json_done_rejected_when_spec_validator_raises(self) -> None:
        from codey.operations import kernel_protocol as kp

        def boom(name, args):
            raise RuntimeError("validator boom")

        with mock.patch("codey.toolchain.tool_spec.validate_args_against_spec", side_effect=boom):
            validated, error = kp._validate_tool_args("done", {"summary": "hi", "undeclared": "x"})
            self.assertEqual(validated, {})
            self.assertTrue(error, "validator exception must return an error, not empty")

            plan = kp.normalize_turn(
                '{"tool":"done","args":{"summary":"hi","undeclared":"x"}}',
                policy=_policy(),
            )
            self.assertIsNone(plan.control, "done must not be accepted when validator raises")
            self.assertTrue(plan.protocol_error, "protocol error must be set")

    def test_native_done_rejected_when_spec_validator_raises(self) -> None:
        from codey.operations import kernel_protocol as kp

        def boom(name, args):
            raise RuntimeError("validator boom")

        call = SimpleNamespace(name="done", arguments={"summary": "hi"}, id="call_1")
        reply = SimpleNamespace(tool_calls=[call], text="")

        with mock.patch("codey.toolchain.tool_spec.validate_args_against_spec", side_effect=boom):
            plan = kp.normalize_turn(reply, policy=_policy())
            self.assertIsNone(plan.control)
            self.assertTrue(plan.protocol_error)

    def test_json_tool_rejected_when_spec_validator_raises(self) -> None:
        from codey.operations import kernel_protocol as kp

        def boom(name, args):
            raise RuntimeError("validator boom")

        with mock.patch("codey.toolchain.tool_spec.validate_args_against_spec", side_effect=boom):
            plan = kp.normalize_turn(
                '{"tool":"read_file","args":{"path":"a.py"}}',
                policy=_policy(),
            )
            self.assertEqual(plan.calls, [])
            self.assertTrue(plan.protocol_error)


if __name__ == "__main__":
    unittest.main()
