"""Persisted session replay keeps workspace provenance and bumps once.

Repro: first ``edit`` bumps the durable store to revision 2 with trusted
identity. A cold restart reopens the real log and revision stores; the
formal recovery entry replays the same edit slot with the same verified
identity, so hooks adopt without a second bump.

Lock: recovered replay carries the same trusted (revision, fingerprint);
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


def _dirs(tmp: Path):
    project = tmp / "project"
    state = tmp / "state"
    logdir = tmp / "log"
    project.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    logdir.mkdir(parents=True, exist_ok=True)
    return project, state, logdir


def _open_runtime(logdir: Path, state: Path, session_id: str, run_id: str, project: Path):
    from codey.operations.task_effects import KernelEffectSink
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
    from codey.runtime.log.session_log import RuntimeSessionLog
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from codey.storage.managed_outputs import ManagedOutputStore
    from codey.workspace.revision import WorkspaceRevisionStore

    log = RuntimeSessionLog(logdir)
    mutations = RuntimeMutationLine(log)
    managed = ManagedOutputStore(state)
    rev_store = WorkspaceRevisionStore(state)
    mutations.accept_operation(
        session_id=session_id, run_id=run_id, project=str(project),
        provider_id="local", turn_budget=20, max_repair_rounds=1, task_kind="project",
    )
    mutations.mark_writer_running(session_id, run_id, provider_id="local")
    sink = KernelEffectSink(
        mutations, session_id=session_id, run_id=run_id, provider_id="local",
        managed_outputs=managed,
    )
    deps = SimpleNamespace(
        runtime_mutations=mutations,
        runtime_effects=RuntimeEffectStore(log),
        tool_result_delivery=ToolResultDeliveryStore(log),
        managed_outputs=managed,
        workspace_revisions=rev_store,
    )
    return log, mutations, managed, rev_store, deps, sink


def _new_session(project: Path):
    from codey.operations.task_session import TaskSession

    return TaskSession(
        policy=_policy(), task_kind="project", project=str(project), max_turns=4
    )


def _recover_formal(project, state, logdir, session_id, run_id):
    from codey.operations.kernel_session_recovery import restore_task_session
    from codey.operations.recovery import recover_effects_for_resume
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
    from codey.runtime.log.session_log import RuntimeSessionLog
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from codey.storage.managed_outputs import ManagedOutputStore
    from codey.workspace.revision import WorkspaceRevisionStore

    log = RuntimeSessionLog(logdir)
    mutations = RuntimeMutationLine(log)
    deps = SimpleNamespace(
        runtime_mutations=mutations,
        runtime_effects=RuntimeEffectStore(log),
        tool_result_delivery=ToolResultDeliveryStore(log),
        managed_outputs=ManagedOutputStore(state),
        workspace_revisions=WorkspaceRevisionStore(state),
    )
    recovery = recover_effects_for_resume(
        deps, session_id=session_id, run_id=run_id, project=str(project), task_kind="project",
    )
    assert recovery.ok is True
    fresh = _new_session(project)
    try:
        cur = deps.workspace_revisions.current_state(str(project), ignored_paths=())
        fresh.set_workspace_state(int(cur.revision or 0), cur.fingerprint)
    except Exception:
        pass
    frame = SimpleNamespace(run_id=run_id, recovered_tool_outcomes=tuple(recovery.recovered_tool_outcomes))
    delivered, rows, resume_start, initial = restore_task_session(frame, fresh)
    return fresh, recovery, delivered, rows, resume_start, deps


class PersistedSessionReplayKeepsWorkspaceProvenanceTests(unittest.TestCase):
    def test_real_log_replay_bumps_exactly_once(self) -> None:
        from codey.operations import kernel_events as kev
        from codey.operations import kernel_execution as ke
        from codey.operations.context import RunWork
        from codey.operations.kernel_protocol import build_turn_snapshot
        from codey.operations.task_phases import hooks as hooks_mod
        from codey.operations.task_session import turn_effect_id
        from codey.runtime.core.models import ToolCall, ToolResult
        from codey.runtime.observe.execution_evidence import ExecutionEvidence
        from codey.workspace.revision import WorkspaceRevisionStore
        from tests.recovery_test_helpers import (
            trusted_workspace_pair as _trusted_workspace_from_result,
        )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            tmp = Path(td)
            project, state, logdir = _dirs(tmp)
            session_id = "s-persist-1"
            run_id = "r-persist-1"
            _, _, _, rev_store, _, sink = _open_runtime(logdir, state, session_id, run_id, project)
            (project / "a.py").write_text("x=1\n", encoding="utf-8")
            session = _new_session(project)
            base = rev_store.current_state(str(project), ignored_paths=())
            evidence = ExecutionEvidence(
                workspace_revision=int(base.revision or 0),
                workspace_fingerprint=base.fingerprint,
            )
            snapshot = build_turn_snapshot(session)

            def fake_edit(call: ToolCall):
                (project / str(call.args.get("path") or "b.py")).write_text(
                    str(call.args.get("content") or "y"), encoding="utf-8"
                )
                return ToolResult(ok=True, call=call, model_text="edited", audit={"changed": True})

            bump_calls: list[str] = []
            orig_bump = WorkspaceRevisionStore.bump_state

            def counting_bump(self, proj, *, ignored_paths=()):
                bump_calls.append(str(proj))
                return orig_bump(self, proj, ignored_paths=ignored_paths)

            with mock.patch.object(WorkspaceRevisionStore, "bump_state", counting_bump):
                results = ke.execute_turn(
                    session,
                    [ToolCall(name="edit", args={"path": "b.py", "content": "y=2\n"})],
                    executors={"edit": fake_edit},
                    run_id=run_id, effect_scope="task", turn=1,
                    project_path=project, intent_sink=sink, snapshot=snapshot,
                    execution_evidence=evidence,
                    workspace_ignored_paths=(),
                    workspace_revision_store=rev_store,
                )
                self.assertEqual(len(results), 1)
                first_rev, first_fp = _trusted_workspace_from_result(results[0])
                self.assertTrue(first_rev and first_fp, f"first edit untrusted: {dict(results[0].audit)!r}")

                # Real restart: reopen stores and recover via the formal entry.
                # The fresh session starts without process-local memory.
                fresh, recovery, delivered, rows, resume_start, _deps2 = _recover_formal(
                    project, state, logdir, session_id, run_id
                )
                self.assertEqual(len(fresh._memory_results), 1)
                self.assertEqual(next(iter(fresh._memory_results.values())).call.name, "edit")
                self.assertEqual(fresh.edited_files, session.edited_files)

                identity = turn_effect_id("r-persist-1:task", 1, 0)
                self.assertIn(identity, delivered)
                replayed = delivered[identity]
                self.assertIsNotNone(replayed, "persisted edit slot must replay")
                assert replayed is not None
                # Successful real recovery must restore the same verified
                # identity (durable store corroboration); never an ERROR.
                replay_text = str(replayed.model_text or "")
                self.assertFalse(
                    replay_text.startswith("ERROR:"),
                    f"verified persisted replay must succeed, got {replay_text!r}",
                )
                replay_rev, replay_fp = _trusted_workspace_from_result(replayed)
                self.assertEqual(replay_rev, first_rev)
                self.assertEqual(replay_fp, first_fp)

                # Re-executing the same slot on the recovered session reuses
                # the durable delivery without invoking the executor again.
                executed_calls: list[str] = []

                def must_not_execute(call: ToolCall):
                    executed_calls.append(str(call.name))
                    return ToolResult(ok=True, call=call, model_text="must-not-run", audit={"changed": True})

                fresh_snapshot = build_turn_snapshot(fresh)
                again = ke.execute_turn(
                    fresh,
                    [ToolCall(name="edit", args={"path": "b.py", "content": "y=2\n"})],
                    executors={"edit": must_not_execute},
                    run_id=run_id, effect_scope="task", turn=1,
                    project_path=project, snapshot=fresh_snapshot,
                    execution_evidence=evidence,
                    workspace_ignored_paths=(),
                    workspace_revision_store=_deps2.workspace_revisions,
                    delivered=delivered,
                )
                self.assertEqual(executed_calls, [])
                self.assertEqual(len(again), 1)
                again_rev, again_fp = _trusted_workspace_from_result(again[0])
                self.assertEqual(again_rev, first_rev)
                self.assertEqual(again_fp, first_fp)

                work = RunWork(
                    recent_events=[], evidence=evidence,
                    workspace_revision=int(base.revision or 0),
                    workspace_fingerprint=base.fingerprint,
                )
                state_ns = SimpleNamespace(
                    emit=lambda _p: None,
                    providers=SimpleNamespace(supervisor=None),
                    self_repair=None,
                    provider_failover_order=lambda: (),
                    add_pending_shell_approval=lambda *a, **k: None,
                )
                deps = SimpleNamespace(work_checkpoints=None, workspace_revisions=_deps2.workspace_revisions)
                hooks = hooks_mod.build_hooks(
                    deps, state_ns, work, session_id="s1", run_id="r-persist-1",
                    project=str(project), max_turns=4, project_config_ignored=(),
                    review_log_lines=80,
                    project_completion_deps=SimpleNamespace(state=state_ns),
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
                    # Replayed event carries the same verified identity and
                    # must not bump again.
                    kev._emit_tool_results(
                        hooks.on_event, fresh, again,
                        run_id="r-persist-1:task", turn=1,
                    )
                self.assertEqual(
                    len(bump_calls), first_bumps,
                    f"replay must not bump again: {bump_calls}",
                )


if __name__ == "__main__":
    unittest.main()
