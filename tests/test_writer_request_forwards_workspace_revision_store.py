"""Project writer requests must carry the durable workspace revision store."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock


class WriterRequestForwardsWorkspaceRevisionStoreTests(unittest.TestCase):
    def test_writer_attempt_forwards_workspace_revision_store(self) -> None:
        from codey.agents.writer_failover import CheckpointView, WriterAttempt
        from codey.operations import project_writer_phase
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.run_result import RunResult

        captured = []
        store = object()

        deps = SimpleNamespace(
            agent=SimpleNamespace(run=lambda request: captured.append(request) or RunResult("ok", "done", 1, True, True)),
            runtime=SimpleNamespace(mutations=None, tool_result_delivery=None),
            persistence=SimpleNamespace(managed_outputs=None),
            verification=SimpleNamespace(workspace_revisions=store),
        )
        frame = SimpleNamespace(
            recovered_tool_outcomes=(),
            recovered_tool_result_batch_id="",
            conversation=None,
            provider_session_changed=False,
            run_id="run-1",
            trace=None,
            project_text="project",
            entry_policy=TaskPolicy(grants=frozenset({"control", "project.write", "project.verify"})),
        )
        ctx = SimpleNamespace(
            writer_attempt_index=0,
            frame=frame,
            state=SimpleNamespace(run_registry=SimpleNamespace(stop_flag=None)),
            request=SimpleNamespace(session_id="session-1", requested_capabilities=()),
            project="project",
            deps=deps,
            verified_facts="",
            project_context=SimpleNamespace(research_context="", project_config_warnings=""),
            project_map="",
            repair_projection=None,
            task_session=None,
            work=SimpleNamespace(evidence=None, analysis_run_payloads=[]),
            verification_forbidden=False,
            configured_ignored_paths=("generated",),
            tracker=None,
            verification_candidates=(),
            hooks=SimpleNamespace(on_shell_request=lambda _approval: None),
        )
        spec = WriterAttempt(
            task="write",
            provider_id="local",
            provider=object(),
            remaining_turns=1,
            fresh_chat=False,
            handoff="",
            checkpoint=CheckpointView(),
        )

        with mock.patch.object(project_writer_phase, "_ghost_experiences", return_value=""), mock.patch.object(
            project_writer_phase, "_build_research_tools", return_value=None
        ), mock.patch.object(project_writer_phase, "managed_tool_fns", return_value=None):
            project_writer_phase._run_one_writer_attempt(ctx, spec, lambda _turn: None)

        self.assertEqual(captured[0].workspace_revision_store, store)
        self.assertEqual(captured[0].workspace_ignored_paths, ("generated",))

    def test_coding_writer_without_workspace_revision_store_fails_before_agent_run(self) -> None:
        from codey.agents.writer_failover import CheckpointView, WriterAttempt
        from codey.operations import project_writer_phase

        run = mock.Mock()
        deps = SimpleNamespace(
            agent=SimpleNamespace(run=run),
            runtime=SimpleNamespace(mutations=None, tool_result_delivery=None),
            persistence=SimpleNamespace(managed_outputs=None),
            verification=SimpleNamespace(workspace_revisions=None),
        )
        frame = SimpleNamespace(
            recovered_tool_outcomes=(),
            recovered_tool_result_batch_id="",
            conversation=None,
            provider_session_changed=False,
            run_id="run-1",
            trace=None,
            project_text="project",
        )
        ctx = SimpleNamespace(
            writer_attempt_index=0,
            frame=frame,
            state=SimpleNamespace(run_registry=SimpleNamespace(stop_flag=None)),
            request=SimpleNamespace(session_id="session-1", requested_capabilities=()),
            project="project",
            deps=deps,
            verified_facts="",
            project_context=SimpleNamespace(research_context="", project_config_warnings=""),
            project_map="",
            repair_projection=None,
            tracker=None,
            verification_candidates=(),
            hooks=SimpleNamespace(on_shell_request=lambda _approval: None),
        )
        spec = WriterAttempt(
            task="write",
            provider_id="local",
            provider=object(),
            remaining_turns=1,
            fresh_chat=False,
            handoff="",
            checkpoint=CheckpointView(),
        )

        with mock.patch.object(project_writer_phase, "_ghost_experiences", return_value=""), self.assertRaises(
            RuntimeError
        ) as raised:
            project_writer_phase._run_one_writer_attempt(ctx, spec, lambda _turn: None)

        self.assertIn("WorkspaceRevisionStore", str(raised.exception))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
