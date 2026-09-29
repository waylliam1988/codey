"""Tampered persisted executed payload must not become trusted provenance.

Repro: ``TaskSession.executed`` accepts any payload with a well-formed
``workspace_revision``/``workspace_fingerprint``. A tampered record such as
``revision=999, fingerprint=sha256:0*64`` is rebuilt by
``_replay_settled_slot`` into a trusted side-channel, so the replayed event
carries 999 and hooks skip the real bump. Format validation is not source
validation.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


def _policy():
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({"project.write", "control"}))


class PersistedExecutedTamperedProvenanceRejectedTests(unittest.TestCase):
    def test_tampered_executed_payload_does_not_replay_as_trusted(self) -> None:
        from codey.operations import kernel_execution as ke
        from codey.operations.task_session import TaskSession, turn_effect_id
        from codey.runtime.core.models import ToolCall
        from codey.workspace.revision import WorkspaceRevisionStore

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            project = Path(td)
            (project / "a.py").write_text("x=1\n", encoding="utf-8")
            store = WorkspaceRevisionStore(Path(home))
            session = TaskSession(
                policy=_policy(), task_kind="project", project=str(project), max_turns=4
            )
            identity = turn_effect_id("r-tamper-1:task", 1, 0)
            call = ToolCall(name="edit", args={"path": "a.py", "content": "x=2\n"}, call_id="c1")
            session.executed[identity] = {
                "name": "edit",
                "ok": True,
                "call_id": "c1",
                "excerpt": "edited",
                "args_digest": ke._call_args_digest(call),
                "workspace_revision": 999,
                "workspace_fingerprint": "sha256:" + "0" * 64,
            }
            # Durable store knows nothing about revision 999: current is 1.
            replayed = ke._replay_settled_slot(
                session, identity, call, "edit", 1,
                project_path=project, revision_store=store,
            ) if "project_path" in ke._replay_settled_slot.__code__.co_varnames else ke._replay_settled_slot(
                session, identity, call, "edit", 1,
            )
            self.assertIsNotNone(replayed)
            assert replayed is not None
            text = str(replayed.model_text or "")
            self.assertTrue(
                text.startswith("ERROR:"),
                f"tampered provenance must fail closed, got {text!r}",
            )
            rev, fp = ke._trusted_workspace_from_result(replayed)
            self.assertEqual((rev, fp), (0, ""), f"tampered replay must be untrusted, got {(rev, fp)!r}")

    def test_tampered_payload_never_executes_executor(self) -> None:
        from codey.operations import kernel_execution as ke
        from codey.operations.task_session import TaskSession, turn_effect_id
        from codey.runtime.core.models import ToolCall
        from codey.workspace.revision import WorkspaceRevisionStore

        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as home:
            project = Path(td)
            (project / "a.py").write_text("x=1\n", encoding="utf-8")
            store = WorkspaceRevisionStore(Path(home))
            session = TaskSession(
                policy=_policy(), task_kind="project", project=str(project), max_turns=4
            )
            call = ToolCall(name="edit", args={"path": "a.py", "content": "x=2\n"}, call_id="c1")
            identity = turn_effect_id("r-tamper-2:task", 1, 0)
            session.executed[identity] = {
                "name": "edit",
                "ok": True,
                "call_id": "c1",
                "excerpt": "edited",
                "args_digest": ke._call_args_digest(call),
                "workspace_revision": 999,
                "workspace_fingerprint": "sha256:" + "0" * 64,
            }
            calls = [False]

            def fake_edit(_c: ToolCall):
                calls[0] = True
                from codey.runtime.core.models import ToolResult
                return ToolResult(call=_c, model_text="edited again")

            kwargs = dict(
                executors={"edit": fake_edit},
                run_id="r-tamper-2", effect_scope="task", turn=1,
                project_path=project, workspace_revision_store=store,
            )
            results = ke.execute_turn(session, [call], **kwargs)
            self.assertEqual(calls[0], False, "tampered replay must not re-execute executor")
            self.assertTrue(str(results[0].model_text or "").startswith("ERROR:"))


if __name__ == "__main__":
    unittest.main()
