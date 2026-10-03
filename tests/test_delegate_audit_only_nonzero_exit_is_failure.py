"""Delegate audit-only nonzero exit must force ok=False.

Repro: ``_normalize_delegate_result`` only consults the structured
``exit_code`` parameter. When a delegate returns ``ok=True``,
``exit_code=None`` but ``audit={"exit_code": 1}``, the run keeps
``ok=True`` while verification records failure, so UI and ledger disagree.
"""
from __future__ import annotations

import unittest


class DelegateAuditOnlyNonzeroExitIsFailureTests(unittest.TestCase):
    def test_audit_exit_1_with_none_param_is_not_ok(self) -> None:
        from codey.operations.kernel_result import _normalize_delegate_result, result_ok
        from codey.runtime.core.models import ToolCall, ToolResult

        call = ToolCall(name="run", args={"command": "x", "path": "."}, call_id="c1")
        result = ToolResult(ok=True, call=call, model_text="done", audit={"exit_code": 1})
        normalized, ok, _code = _normalize_delegate_result("run", result, True, None)
        self.assertFalse(ok, f"audit exit 1 must force ok=False, got ok={ok!r} audit={dict(normalized.audit)!r}")
        self.assertFalse(
            result_ok("run", normalized),
            "normalized run result with audit exit 1 must not be ok",
        )


if __name__ == "__main__":
    unittest.main()
