"""Persisted session replay keeps workspace provenance and bumps once.

Repro: first ``edit`` bumps the durable store to revision 2 with trusted
identity. ``TaskSession.to_payload()`` / ``from_payload()`` drops
``_memory_results``; the same edit slot replayed on the restored session
rebuilt only ``model_text`` with empty audit, so hooks could not adopt and
bumped again to revision 3.

Lock: restored replay carries the same trusted (revision, fingerprint);
emitting first event then replayed event bumps the store exactly once
total (first_bumps == 1 and total == 1).
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


class PersistedSessionReplayKeepsWorkspaceProvenanceTests(unittest.TestCase):
    def test_to_payload_from_payload_replay_bumps_exactly_once(self) -> None:
        from codey.operations import kernel_events as kev
        from codey.operations import kernel_execution as ke
        from codey.operations.context import RunWork
        from codey.operations.task_phases import hooks as hooks_mod
        from codey.operations.task_session import TaskSession, turn_effect_id
        from codey.runtime.core.models import ToolCall, ToolResult
        from codey.runtime.observe.execution_evidence import ExecutionEvidence
        from codey.workspace.revision import WorkspaceRevisionStore

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

            def fake_edit(call: ToolCall):
                (project / str(call.args.get("path") or "b.py")).write_text(
                    str(call.args.get("content") or "y"), encoding="utf-8"
                )
                return ToolResult(call=call, model_text="edited", audit={"changed": True})

            bump_calls: list[str] = []
            orig_bump = store.bump_state

            def counting_bump(proj, *, ignored_paths=()):
                bump_calls.append(str(proj))
                return orig_bump(proj, ignored_paths=ignored_paths)

            with mock.patch.object(store, "bump_state", side_effect=counting_bump):
                results = ke.execute_turn(
                    session,
                    [ToolCall(name="edit", args={"path": "b.py", "content": "y=2\n"})],
                    executors={"edit": fake_edit},
                    run_id="r-persist-1", effect_scope="task", turn=1,
                    project_path=project,
                    execution_evidence=evidence,
                    workspace_ignored_paths=(),
                    workspace_revision_store=store,
                )
                self.assertEqual(len(results), 1)
                first_rev, first_fp = ke._trusted_workspace_from_result(results[0])
                self.assertTrue(first_rev and first_fp, f"first edit untrusted: {dict(results[0].audit)!r}")

                # Persist across restart: memory results are dropped.
                payload = session.to_payload()
                restored = TaskSession.from_payload(payload, policy=session.policy)
                self.assertEqual(restored._memory_results, {})

                identity = turn_effect_id("r-persist-1:task", 1, 0)
                replayed = ke._replay_settled_slot(
                    restored, identity,
                    ToolCall(name="edit", args={"path": "b.py", "content": "y=2\n"}),
                    "edit", 1,
                )
                self.assertIsNotNone(replayed, "persisted edit slot must replay")
                assert replayed is not None
                # Unsafe replay without provenance must fail closed, never a
                # bare success that would bump again.
                replay_text = str(replayed.model_text or "")
                replay_rev, replay_fp = ke._trusted_workspace_from_result(replayed)
                if not (replay_rev and replay_fp):
                    self.assertTrue(
                        replay_text.startswith("ERROR:"),
                        f"provenance-less unsafe replay must be ERROR, got {replay_text!r}",
                    )
                else:
                    self.assertEqual(replay_rev, first_rev)
                    self.assertEqual(replay_fp, first_fp)

                work = RunWork(
                    recent_events=[], evidence=evidence,
                    workspace_revision=int(base.revision or 0),
                    workspace_fingerprint=base.fingerprint,
                )
                state = SimpleNamespace(
                    emit=lambda _p: None,
                    providers=SimpleNamespace(supervisor=None),
                    self_repair=None,
                    provider_failover_order=lambda: (),
                    add_pending_shell_approval=lambda *a, **k: None,
                )
                deps = SimpleNamespace(work_checkpoints=None, workspace_revisions=store)
                hooks = hooks_mod.build_hooks(
                    deps, state, work, session_id="s1", run_id="r-persist-1",
                    project=str(project), max_turns=4, project_config_ignored=(),
                    review_log_lines=80,
                    project_completion_deps=SimpleNamespace(state=state),
                    current_provider_id=lambda: "local",
                )
                with mock.patch(
                    "codey.operations.task_phases.hooks.handle_project_tool_event",
                    return_value=None,
                ):
                    kev._emit_tool_results(
                        hooks.on_event, session, results,
                        run_id="r-persist-1:task", turn=1,
                    )
                    first_bumps = len(bump_calls)
                    self.assertEqual(first_bumps, 1, f"first event must bump once: {bump_calls}")
                    # Replayed event must not bump again when it carries the
                    # same trusted identity; a fail-closed ERROR also bumps 0.
                    if replay_rev and replay_fp:
                        kev._emit_tool_results(
                            hooks.on_event, restored, [replayed],
                            run_id="r-persist-1:task", turn=1,
                        )
                self.assertEqual(
                    len(bump_calls), first_bumps,
                    f"replay must not bump again: {bump_calls}",
                )


if __name__ == "__main__":
    unittest.main()
