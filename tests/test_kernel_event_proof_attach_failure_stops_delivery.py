"""A trusted edit event must not fall back to a second workspace bump."""
from __future__ import annotations

import unittest
from unittest import mock


class KernelEventProofAttachFailureStopsDeliveryTests(unittest.TestCase):
    def test_proof_attach_failure_is_not_suppressed(self) -> None:
        from codey.operations import kernel_events
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.kernel_provenance import _KERNEL_WORKSPACE_ATTR
        from codey.operations.task_session import TaskSession, turn_effect_id
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall, ToolResult
        from codey.workspace.revision import WorkspaceIdentity

        session = TaskSession(
            policy=TaskPolicy(grants=frozenset({"project.write", "control"})),
            task_kind="project",
            project="p",
            max_turns=4,
        )
        call = ToolCall(name="edit", args={"path": "a.py", "content": "x"}, call_id="c1")
        result = ToolResult(call=call, model_text="edited", audit={"changed": True})
        identity = WorkspaceIdentity.trusted_pair(2, "sha256:" + "ab" * 32)
        object.__setattr__(result, _KERNEL_WORKSPACE_ATTR, identity)
        session.executed[turn_effect_id("run:task", 1, 0)] = {"ok": True}
        events = []

        with mock.patch(
            "codey.operations.kernel_provenance.attach_proof_to_event",
            side_effect=RuntimeError("proof channel unavailable"),
        ), self.assertRaises(RecoveryFailed):
            kernel_events._emit_tool_results(
                events.append,
                session,
                [result],
                run_id="run:task",
                turn=1,
            )

        self.assertEqual(events, [])

    def test_provenance_read_failure_is_not_downgraded_to_untrusted_event(self) -> None:
        from codey.operations import kernel_events
        from codey.operations.kernel_errors import RecoveryFailed
        from codey.operations.kernel_provenance import _KERNEL_WORKSPACE_ATTR
        from codey.operations.task_session import TaskSession, turn_effect_id
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall, ToolResult
        from codey.workspace.revision import WorkspaceIdentity

        session = TaskSession(
            policy=TaskPolicy(grants=frozenset({"project.write", "control"})),
            task_kind="project",
            project="p",
            max_turns=4,
        )
        call = ToolCall(name="edit", args={"path": "a.py", "content": "x"}, call_id="c1")
        result = ToolResult(call=call, model_text="edited", audit={"changed": True})
        object.__setattr__(
            result,
            _KERNEL_WORKSPACE_ATTR,
            WorkspaceIdentity.trusted_pair(2, "sha256:" + "ab" * 32),
        )
        session.executed[turn_effect_id("run:task", 1, 0)] = {"ok": True}
        events = []

        with mock.patch(
            "codey.operations.kernel_provenance._kernel_workspace_identity_of",
            side_effect=RuntimeError("provenance read failed"),
        ), self.assertRaises(RecoveryFailed):
            kernel_events._emit_tool_results(
                events.append,
                session,
                [result],
                run_id="run:task",
                turn=1,
            )

        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
