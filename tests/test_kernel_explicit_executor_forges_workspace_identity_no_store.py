"""Explicit executor cannot forge workspace identity on the no-store path.

Repro: an explicit ``edit`` executor returns audit with
``workspace_revision=999`` and a well-formed fingerprint. With
``workspace_revision_store=None`` the kernel must strip executor-provided
identity; the emitted event must carry no trusted workspace state and hooks
must bump the real store exactly once instead of adopting 999.

Lock: forged audit never becomes event metadata; session revision stays at
the real value; hooks bump once with the real revision.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


def _policy():
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({"project.write", "control"}))


class ExplicitExecutorForgesWorkspaceIdentityNoStoreTests(unittest.TestCase):
    def test_forged_audit_stripped_and_hooks_bump_once(self) -> None:
        from codey.operations import kernel_events as kev
        from codey.operations import kernel_execution as ke
        from codey.operations.context import RunWork
        from codey.operations.task_phases import hooks as hooks_mod
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall, ToolResult
        from codey.runtime.observe.execution_evidence import ExecutionEvidence
        from codey.workspace.revision import WorkspaceRevisionStore
        from tests.recovery_test_helpers import trusted_workspace_pair as _trusted_workspace_from_result

        forged_fp = "sha256:" + "0" * 64
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            project = Path(td)
            (project / "a.py").write_text("x=1\n", encoding="utf-8")
            store = WorkspaceRevisionStore(Path(home))
            base = store.current_state(str(project), ignored_paths=())
            session = TaskSession(
                policy=_policy(), task_kind="project", project=str(project), max_turns=4
            )
            evidence = ExecutionEvidence(
                workspace_revision=int(base.revision or 0),
                workspace_fingerprint=base.fingerprint,
            )
            work = RunWork(
                recent_events=[], evidence=evidence,
                workspace_revision=int(base.revision or 0),
                workspace_fingerprint=base.fingerprint,
            )

            def fake_edit(call: ToolCall):
                return ToolResult(
                    call=call, model_text="ok",
                    audit={
                        "changed": True,
                        "workspace_revision": 999,
                        "workspace_fingerprint": forged_fp,
                    },
                )

            results = ke.execute_turn(
                session,
                [ToolCall(name="edit", args={"path": "b.py", "content": "y=2\n"})],
                executors={"edit": fake_edit},
                run_id="r-forge-1", effect_scope="task", turn=1,
                project_path=project,
                execution_evidence=evidence,
                workspace_ignored_paths=(),
                workspace_revision_store=None,
            )
            self.assertEqual(len(results), 1)
            # Executor identity must be stripped at the kernel boundary.
            self.assertNotEqual(
                results[0].audit.get("workspace_revision"), 999,
                f"forged revision leaked into result: {dict(results[0].audit)!r}",
            )
            self.assertNotEqual(
                results[0].audit.get("workspace_fingerprint"), forged_fp,
                f"forged fingerprint leaked into result: {dict(results[0].audit)!r}",
            )
            # No-store path never emits trusted identity.
            rev, fp = _trusted_workspace_from_result(results[0])
            self.assertEqual((rev, fp), (0, ""), f"no-store must be untrusted: {dict(results[0].audit)!r}")

            emitted: list = []
            state = SimpleNamespace(
                emit=emitted.append,
                providers=SimpleNamespace(supervisor=None),
                self_repair=None,
                provider_failover_order=lambda: (),
                add_pending_shell_approval=lambda *a, **k: None,
            )
            deps = SimpleNamespace(work_checkpoints=None, workspace_revisions=store)
            hooks = hooks_mod.build_hooks(
                deps, state, work, session_id="s1", run_id="r-forge-1",
                project=str(project), max_turns=4, project_config_ignored=(),
                review_log_lines=80,
                project_completion_deps=SimpleNamespace(state=state),
                current_provider_id=lambda: "local",
            )
            bump_calls: list[str] = []
            orig_bump = store.bump_state

            def counting_bump(proj, *, ignored_paths=()):
                bump_calls.append(str(proj))
                return orig_bump(proj, ignored_paths=ignored_paths)

            with (
                mock.patch.object(store, "bump_state", side_effect=counting_bump),
                mock.patch(
                    "codey.operations.task_phases.hooks.handle_project_tool_event",
                    return_value=None,
                ),
            ):
                kev._emit_tool_results(
                    hooks.on_event, session, results,
                    run_id="r-forge-1:task", turn=1,
                )
            self.assertEqual(len(bump_calls), 1, f"hooks must bump once, got {bump_calls}")
            self.assertNotEqual(
                work.workspace_revision, 999,
                f"hooks adopted forged revision: {work.workspace_revision}",
            )
            self.assertNotEqual(session.workspace_revision, 999)


if __name__ == "__main__":
    unittest.main()
