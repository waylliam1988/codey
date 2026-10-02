"""Workspace bump failure never reuses the old revision as new evidence.

Repro: sync_workspace_state_after_edit() returned (0, "") on bump_state()
failure, but execute_turn() discarded it. kernel_events then read the
session's current revision/fingerprint (old revision 5 + new file
fingerprint) into event metadata; hooks saw two non-empty fields and
adopted without a second bump. Old revision was presented as new state.

Lock: execute_turn() captures the successful (rev, fp) per edit into the
trusted result metadata; kernel_events emits only that trusted pair, never
the session guess. On bump failure the edit is reported as
"edit happened, workspace identity unconfirmed": same-batch later
verification (run) is not executed, the batch returns an explicit failure,
and recovery must re-check the workspace instead of re-doing the edit.
Old revision is never emitted as new evidence.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


def _policy():
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({"project.write", "control"}))


class WorkspaceBumpFailureIdentityTests(unittest.TestCase):
    def test_bump_error_does_not_emit_old_revision_as_new(self) -> None:
        from codey.operations import kernel_events as kev
        from codey.operations import kernel_execution as ke
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall, ToolResult
        from codey.workspace.revision import workspace_fingerprint

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            project = Path(td)
            (project / "a.py").write_text("x=1\n", encoding="utf-8")
            from codey.workspace.revision import WorkspaceRevisionStore

            store = WorkspaceRevisionStore(Path(home))
            base = store.current_state(str(project), ignored_paths=())

            session = TaskSession(policy=_policy(), task_kind="project", project=str(project), max_turns=4)
            session.set_workspace_state(5, base.fingerprint)

            class _BoomStore:
                def bump_state(self, proj, *, ignored_paths=()):
                    raise OSError("disk gone")

            def fake_edit(call: ToolCall):
                (project / "b.py").write_text("y=2\n", encoding="utf-8")
                return ToolResult(call=call, model_text="edited", audit={"changed": True})

            results = ke.execute_turn(
                session,
                [ToolCall(name="edit", args={"path": "b.py", "content": "y=2\n"})],
                executors={"edit": fake_edit},
                run_id="r-bump-fail-1",
                effect_scope="task",
                turn=1,
                project_path=project,
                execution_evidence=None,
                workspace_ignored_paths=(),
                workspace_revision_store=_BoomStore(),
            )
            self.assertEqual(len(results), 1)
            # Failure is explicit, not a silent ok edit.
            self.assertTrue(str(results[0].model_text).startswith("ERROR:"), results[0].model_text[:200])
            events: list = []
            kev._emit_tool_results(events.append, session, results, run_id="r-bump-fail-1:task", turn=1)
            for ev in events:
                meta = dict(getattr(ev, "metadata", {}) or {})
                self.assertFalse(
                    meta.get("workspace_revision") and meta.get("workspace_fingerprint"),
                    f"failed bump must not emit trusted workspace state: {meta}",
                )
            # Session must not look like "revision 5 + new fingerprint".
            sess_rev = int(getattr(session, "workspace_revision", 0) or 0)
            sess_fp = str(getattr(session, "workspace_fingerprint", "") or "")
            disk_fp = workspace_fingerprint(project)
            if disk_fp and sess_fp == disk_fp:
                self.assertNotEqual(sess_rev, 5, "old revision must not be paired with the new fingerprint")

    def test_missing_store_does_not_claim_new_revision(self) -> None:
        from codey.operations import kernel_events as kev
        from codey.operations import kernel_execution as ke
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall, ToolResult

        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            (project / "a.py").write_text("x=1\n", encoding="utf-8")
            session = TaskSession(policy=_policy(), task_kind="project", project=str(project), max_turns=4)
            session.set_workspace_state(5, "sha256:" + "0" * 64)

            def fake_edit(call: ToolCall):
                (project / "b.py").write_text("y=2\n", encoding="utf-8")
                return ToolResult(call=call, model_text="edited", audit={"changed": True})

            results = ke.execute_turn(
                session,
                [ToolCall(name="edit", args={"path": "b.py", "content": "y=2\n"})],
                executors={"edit": fake_edit},
                run_id="r-no-store-1",
                effect_scope="task",
                turn=1,
                project_path=project,
                execution_evidence=None,
                workspace_ignored_paths=(),
                workspace_revision_store=None,
            )
            events: list = []
            kev._emit_tool_results(events.append, session, results, run_id="r-no-store-1:task", turn=1)
            for ev in events:
                meta = dict(getattr(ev, "metadata", {}) or {})
                self.assertFalse(
                    meta.get("workspace_revision") and meta.get("workspace_fingerprint"),
                    f"no-store edit must not emit trusted revision: {meta}",
                )

    def test_same_batch_edit_then_run_does_not_verify_after_bump_failure(self) -> None:
        from codey.operations import kernel_execution as ke
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall, ToolResult

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            project = Path(td)
            (project / "a.py").write_text("x=1\n", encoding="utf-8")
            from codey.workspace.revision import WorkspaceRevisionStore

            store = WorkspaceRevisionStore(Path(home))
            base = store.current_state(str(project), ignored_paths=())
            session = TaskSession(policy=_policy(), task_kind="project", project=str(project), max_turns=4)
            session.set_workspace_state(5, base.fingerprint)

            class _BoomStore:
                def bump_state(self, proj, *, ignored_paths=()):
                    raise OSError("disk gone")

            run_calls: list[str] = []

            def fake_edit(call: ToolCall):
                (project / "b.py").write_text("y=2\n", encoding="utf-8")
                return ToolResult(call=call, model_text="edited", audit={"changed": True})

            def fake_run(call: ToolCall):
                run_calls.append(str(call.args.get("command") or "run"))
                return ToolResult(call=call, model_text="ok", audit={"exit_code": 0})

            results = ke.execute_turn(
                session,
                [
                    ToolCall(name="edit", args={"path": "b.py", "content": "y=2\n"}),
                    ToolCall(name="run", args={"command": "python -m unittest discover", "path": "."}),
                ],
                executors={"edit": fake_edit, "run": fake_run},
                run_id="r-batch-fail-1",
                effect_scope="task",
                turn=1,
                project_path=project,
                execution_evidence=None,
                workspace_ignored_paths=(),
                workspace_revision_store=_BoomStore(),
            )
            self.assertEqual(len(results), 2)
            self.assertTrue(str(results[0].model_text).startswith("ERROR:"), results[0].model_text[:200])
            # The follow-up verification in the same batch never ran.
            self.assertEqual(run_calls, [], f"run must be skipped after unconfirmed edit: {run_calls}")
            self.assertTrue(str(results[1].model_text).startswith("ERROR:"), results[1].model_text[:200])
            self.assertEqual(session.verifications, [], f"no verification may be recorded: {session.verifications}")

    def test_consecutive_edits_each_carry_their_own_trusted_revision(self) -> None:
        from codey.operations import kernel_events as kev
        from codey.operations import kernel_execution as ke
        from codey.operations.task_session import TaskSession
        from codey.runtime.core.models import ToolCall, ToolResult
        from codey.workspace.revision import WorkspaceRevisionStore

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            project = Path(td)
            (project / "a.py").write_text("x=1\n", encoding="utf-8")
            store = WorkspaceRevisionStore(Path(home))
            base = store.current_state(str(project), ignored_paths=())
            session = TaskSession(policy=_policy(), task_kind="project", project=str(project), max_turns=4)
            session.set_workspace_state(int(base.revision or 0), base.fingerprint)

            def fake_edit(call: ToolCall):
                (project / str(call.args.get("path") or "x.py")).write_text(
                    str(call.args.get("content") or "x"), encoding="utf-8"
                )
                return ToolResult(call=call, model_text="edited", audit={"changed": True})

            seen: list[tuple[int, str]] = []
            for turn, name in ((1, "n1.py"), (2, "n2.py")):
                results = ke.execute_turn(
                    session,
                    [ToolCall(name="edit", args={"path": name, "content": "x=2\n"})],
                    executors={"edit": fake_edit},
                    run_id="r-consec-1",
                    effect_scope="task",
                    turn=turn,
                    project_path=project,
                    execution_evidence=None,
                    workspace_ignored_paths=(),
                    workspace_revision_store=store,
                )
                events: list = []
                kev._emit_tool_results(events.append, session, results, run_id="r-consec-1:task", turn=turn)
                meta = dict(getattr(events[0], "metadata", {}) or {})
                self.assertTrue(meta.get("workspace_revision"), meta)
                self.assertTrue(meta.get("workspace_fingerprint"), meta)
                seen.append((int(meta["workspace_revision"]), str(meta["workspace_fingerprint"])))
            self.assertNotEqual(seen[0][0], seen[1][0], f"consecutive edits must advance: {seen}")


if __name__ == "__main__":
    unittest.main()
