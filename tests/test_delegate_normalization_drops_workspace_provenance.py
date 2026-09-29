"""Delegate normalization must not copy trusted workspace side-channels."""
from __future__ import annotations

import unittest


class DelegateNormalizationDropsWorkspaceProvenanceTests(unittest.TestCase):
    def test_invalid_delegate_audit_does_not_copy_workspace_identity(self) -> None:
        from codey.operations.kernel_provenance import _KERNEL_WORKSPACE_ATTR, _kernel_workspace_identity_of
        from codey.operations.kernel_result import _normalize_delegate_result
        from codey.runtime.core.models import ToolCall, ToolResult
        from codey.workspace.revision import WorkspaceIdentity

        call = ToolCall(name="run", args={"command": "echo hi"}, call_id="c1")
        result = ToolResult(call=call, model_text="ok", audit={"exit_code": False})
        object.__setattr__(result, _KERNEL_WORKSPACE_ATTR, WorkspaceIdentity.trusted_pair(7, "sha256:" + "ab" * 32))

        normalized, ok, _ = _normalize_delegate_result("run", result, True, None)

        self.assertFalse(ok)
        self.assertIsNone(_kernel_workspace_identity_of(normalized))


if __name__ == "__main__":
    unittest.main()
