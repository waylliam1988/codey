"""Shared call_arg helper must stay single-sourced and deterministic."""

from __future__ import annotations

import unittest

from codey.agents import tool_execution as tool_execution_module
from codey.runtime.core.models import ToolCall
from codey.toolchain import definition as definition_module


class CallArgSharedTests(unittest.TestCase):
    def test_both_helpers_agree_on_edge_cases(self) -> None:
        cases = [
            (ToolCall("read", {"path": "a.py"}), "path", "", "a.py"),
            (ToolCall("read", {}), "path", ".", "."),
            (ToolCall("read", {"path": None}), "path", ".", "."),
            (ToolCall("run", {"command": 123}), "command", "", "123"),
            (ToolCall("run", {"command": ""}), "command", "fallback", ""),
        ]
        for call, name, default, expected in cases:
            with self.subTest(call=call, name=name):
                self.assertEqual(tool_execution_module.call_arg(call, name, default), expected)
                self.assertEqual(definition_module._call_arg(call, name, default), expected)

    def test_tool_execution_reuses_definition_helper(self) -> None:
        self.assertIs(tool_execution_module.call_arg, definition_module.call_arg)


class CandidateScopeRefTests(unittest.TestCase):
    def test_scope_ref_property_matches_legacy_helpers(self) -> None:
        from codey.ghost import hebbian as hebbian_module
        from codey.ghost import inbox as inbox_module
        from codey.ghost.inbox import GhostMemoryCandidate

        base = {
            "id": "gmc_test",
            "candidate_type": "preference",
            "signal_kind": "style_preference",
            "status": "candidate",
            "summary": "Prefer concise replies.",
            "evidence_quote": "短一点",
            "confidence": 0.9,
            "conflict_key": "reply_length",
            "value_key": "concise",
            "session_id": "s1",
            "run_id": "r1",
            "project": "proj",
            "created_at": "t",
            "updated_at": "t",
            "gate_reason": "ok",
        }
        for scope, expected in (("project", "proj"), ("session", "s1"), ("user", "")):
            with self.subTest(scope=scope):
                candidate = GhostMemoryCandidate(scope=scope, **base)
                self.assertEqual(candidate.scope_ref, expected)
                self.assertEqual(inbox_module._scope_ref(candidate), expected)
                self.assertEqual(hebbian_module._scope_ref_for_candidate(candidate), expected)


if __name__ == "__main__":
    unittest.main()
