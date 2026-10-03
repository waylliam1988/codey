"""_consistent_tool_result always normalizes to the requested call.

Repro: requesting ``read_file`` while the executor returns ``READ_FILE``
passed the lowercase validation but the function returned the executor's
divergent call object. Digest computation failure returned "" for both
sides so "" == "" passed as a match.

Lock: validation success always rebuilds ``ToolResult(call=requested)``
with executor audit content; digest-unavailable is a mismatch error;
the divergent passthrough branch is gone.
"""
from __future__ import annotations

import unittest
from unittest import mock


class ConsistentToolResultNormalizesIdentityTests(unittest.TestCase):
    def test_case_variant_returns_requested_call(self) -> None:
        from codey.operations.kernel_result import _consistent_tool_result
        from codey.runtime.core.models import ToolCall, ToolResult

        requested = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
        produced = ToolResult(
            ok=True, call=ToolCall(name="READ_FILE", args={"path": "a.py"}, call_id="c1"),
            model_text="hi",
            audit={"extra": "keep"},
        )
        out = _consistent_tool_result(requested, produced)
        self.assertIs(out.call, requested, f"must normalize to requested: {out.call!r}")
        self.assertEqual(out.call.name, "read_file")
        self.assertEqual(out.model_text, "hi")
        self.assertEqual(dict(out.audit).get("extra"), "keep")

    def test_digest_failure_is_mismatch_not_match(self) -> None:
        from codey.operations.kernel_result import _consistent_tool_result
        from codey.runtime.core.models import ToolCall, ToolResult

        requested = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
        produced = ToolResult(
            ok=True, call=ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1"),
            model_text="hi",
        )
        with mock.patch(
            "codey.runtime.effects.effect_records.compute_args_digest",
            side_effect=RuntimeError("digest boom"),
        ):
            out = _consistent_tool_result(requested, produced)
        self.assertTrue(
            str(out.model_text or "").startswith("ERROR:"),
            f"digest-unavailable must be mismatch error: {out.model_text!r}",
        )
        self.assertIs(out.call, requested)

    def test_args_mismatch_is_error_with_requested_call(self) -> None:
        from codey.operations.kernel_result import _consistent_tool_result
        from codey.runtime.core.models import ToolCall, ToolResult

        requested = ToolCall(name="read_file", args={"path": "a.py"}, call_id="c1")
        produced = ToolResult(
            ok=True, call=ToolCall(name="read_file", args={"path": "b.py"}, call_id="c1"),
            model_text="hi",
        )
        out = _consistent_tool_result(requested, produced)
        self.assertTrue(str(out.model_text or "").startswith("ERROR:"))
        self.assertIs(out.call, requested)


if __name__ == "__main__":
    unittest.main()
