"""工作区版本单次推进：一次真实编辑恰好一次 bump_state。

生产链：execute_turn（sync_workspace_state_after_edit 唯一推进）
  -> kernel_events._emit_tool_results（把 revision/fingerprint 写入事件 metadata）
  -> task_phases.hooks.on_event（只采纳，不再推进）。

非内核编辑（事件无 workspace metadata）仍由 hooks 推进。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock


class SingleBumpAcrossKernelAndHooksTests(unittest.TestCase):
    def test_single_edit_bumps_exactly_once_across_kernel_and_hooks(self) -> None:
        from codey.operations import kernel_events as kev
        from codey.operations import kernel_execution as ke
        from codey.operations.context import RunWork
        from codey.operations.task_phases import hooks as hooks_mod
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall, ToolResult
        from codey.runtime.observe.execution_evidence import ExecutionEvidence
        from codey.workspace.revision import WorkspaceRevisionStore

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            project = Path(td)
            (project / "a.py").write_text("x=1\n", encoding="utf-8")
            store = WorkspaceRevisionStore(Path(home))
            base = store.current_state(str(project), ignored_paths=())
            base_rev = int(base.revision or 0)

            bump_calls: list[str] = []
            orig_bump = store.bump_state

            def counting_bump(proj, *, ignored_paths=()):
                bump_calls.append(str(proj))
                return orig_bump(proj, ignored_paths=ignored_paths)

            policy = TaskPolicy(grants=frozenset({"project.write", "control"}))
            session = TaskSession(policy=policy, task_kind="project", project=str(project), max_turns=4)
            evidence = ExecutionEvidence(
                workspace_revision=base_rev,
                workspace_fingerprint=base.fingerprint,
            )
            work = RunWork(
                recent_events=[],
                evidence=evidence,
                workspace_revision=base_rev,
                workspace_fingerprint=base.fingerprint,
            )

            def fake_edit(call: ToolCall):
                target = project / str(call.args.get("path") or "b.py")
                target.write_text(str(call.args.get("content") or "y=2\n"), encoding="utf-8")
                return ToolResult(call=call, model_text="edited", audit={"changed": True})

            def production_on_event(event):
                if hooks_mod._workspace_edit_event(event):
                    if not hooks_mod._adopt_kernel_workspace_state(work, event):
                        work.advance_workspace_revision(store, str(project), ignored_paths=())

            with mock.patch.object(store, "bump_state", side_effect=counting_bump):
                results = ke.execute_turn(
                    session,
                    [ToolCall(name="edit", args={"path": "b.py", "content": "y=2\n"})],
                    executors={"edit": fake_edit},
                    run_id="r1",
                    effect_scope="task",
                    turn=1,
                    project_path=project,
                    tool_fns=None,
                    research_tools=None,
                    execution_evidence=evidence,
                    workspace_ignored_paths=(),
                    workspace_revision_store=store,
                )
                kev._emit_tool_results(production_on_event, session, results, run_id="r1:task", turn=1)
            self.assertEqual(len(results), 1)
            self.assertEqual(len(bump_calls), 1, f"single edit must bump once total, got {bump_calls}")
            sess_rev = int(getattr(session, "workspace_revision", 0) or 0)
            sess_fp = str(getattr(session, "workspace_fingerprint", "") or "")
            self.assertEqual(sess_rev, int(getattr(evidence, "workspace_revision", 0) or 0))
            self.assertEqual(sess_rev, int(getattr(work, "workspace_revision", 0) or 0))
            self.assertEqual(sess_fp, str(getattr(evidence, "workspace_fingerprint", "") or ""))
            self.assertEqual(sess_fp, str(getattr(work, "workspace_fingerprint", "") or ""))
            stored = store.current_state(str(project), ignored_paths=())
            self.assertEqual(sess_rev, int(stored.revision or 0))
            self.assertEqual(sess_fp, str(stored.fingerprint or ""))

    def test_consecutive_edits_and_ignored_paths_single_bump_each(self) -> None:
        from codey.operations import kernel_events as kev
        from codey.operations import kernel_execution as ke
        from codey.operations.context import RunWork
        from codey.operations.task_phases import hooks as hooks_mod
        from codey.operations.task_session import TaskSession
        from codey.policies.task_policy import TaskPolicy
        from codey.runtime.core.models import ToolCall, ToolResult
        from codey.runtime.observe.execution_evidence import ExecutionEvidence
        from codey.workspace.revision import WorkspaceRevisionStore

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            project = Path(td)
            (project / "keep.py").write_text("x=1\n", encoding="utf-8")
            (project / "gen").mkdir()
            (project / "gen" / "out.py").write_text("v1\n", encoding="utf-8")
            store = WorkspaceRevisionStore(Path(home))
            base = store.current_state(str(project), ignored_paths=("gen",))
            bump_calls: list[tuple[str, tuple]] = []
            orig_bump = store.bump_state

            def counting_bump(proj, *, ignored_paths=()):
                bump_calls.append((str(proj), tuple(ignored_paths or ())))
                return orig_bump(proj, ignored_paths=ignored_paths)

            policy = TaskPolicy(grants=frozenset({"project.write", "control"}))
            session = TaskSession(policy=policy, task_kind="project", project=str(project), max_turns=4)
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
                target = project / str(call.args.get("path") or "keep.py")
                target.write_text(str(call.args.get("content") or "x"), encoding="utf-8")
                return ToolResult(call=call, model_text="edited", audit={"changed": True})

            def production_on_event(event):
                if hooks_mod._workspace_edit_event(event):
                    if not hooks_mod._adopt_kernel_workspace_state(work, event):
                        work.advance_workspace_revision(store, str(project), ignored_paths=("gen",))

            with mock.patch.object(store, "bump_state", side_effect=counting_bump):
                for turn, (fname, content) in ((1, ("n1.py", "x=2\n")), (2, ("n2.py", "x=3\n"))):
                    results = ke.execute_turn(
                        session,
                        [ToolCall(name="edit", args={"path": fname, "content": content})],
                        executors={"edit": fake_edit},
                        run_id="r1", effect_scope="task", turn=turn,
                        project_path=project, tool_fns=None, research_tools=None,
                        execution_evidence=evidence,
                        workspace_ignored_paths=("gen",),
                        workspace_revision_store=store,
                    )
                    kev._emit_tool_results(production_on_event, session, results, run_id="r1:task", turn=turn)
                (project / "gen" / "out.py").write_text("v2 ignored\n", encoding="utf-8")
            self.assertEqual(len(bump_calls), 2, f"two edits must bump twice total, got {bump_calls}")
            for _, ignores in bump_calls:
                self.assertIn("gen", ignores)
            self.assertEqual(
                int(getattr(session, "workspace_revision", 0) or 0),
                int(getattr(work, "workspace_revision", 0) or 0),
            )


if __name__ == "__main__":
    unittest.main()
