"""Real log+receipt recovery via the formal entry path.

Layout per case (cold start, no compat):
  tmp/project/  project files
  tmp/state/    workspace revisions + managed outputs
  tmp/log/      session log

Flow:
  open RuntimeSessionLog + ManagedOutputStore + WorkspaceRevisionStore +
  KernelEffectSink -> execute_turn edit + run(s) -> record counts/revision/
  fingerprint/call ids -> reopen all stores (restart) ->
  recover_effects_for_resume -> formal kernel_session_recovery.restore_task_session into a
  fresh TaskSession (no manual file rewrite, no hand-made identity) ->
  real completion_gate.evaluate.

Locks:
  fresh success recovers and completes
  unknown exit recovers and blocks
  file change after verification blocks
  forged executor identity never becomes trusted
  native call ids survive
  second restart replays stably without re-execution
  multi-turn legal accumulation keeps all observations
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

from codey.operations.task_loop import KernelExecutionDeps, KernelObservationDeps, KernelRunRequest, KernelTransportDeps


def _dirs(tmp: Path):
    project = tmp / "project"
    state = tmp / "state"
    logdir = tmp / "log"
    project.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    logdir.mkdir(parents=True, exist_ok=True)
    return project, state, logdir


def _policy():
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"}))


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

    return TaskSession(policy=_policy(), task_kind="project", project=str(project), max_turns=8)


def _settle_edit_then_runs(project, state, logdir, session_id, run_id, run_audits, *, forged_run_audit=None):
    """Real edit + runs in one legal turn via the kernel (single settlement).

    One turn keeps the durable operation state machine legal
    (tool_delivery_pending -> next turn requires delivery settlement which
    only the full kernel loop performs). Multi-turn accumulation is covered
    separately via run_task_kernel; here the focus is trusted workspace
    provenance through the real stores + shared restore_task_session.
    """
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.runtime.core.models import ToolCall, ToolResult
    from codey.runtime.observe.execution_evidence import ExecutionEvidence

    _, _, managed, rev_store, _, sink = _open_runtime(logdir, state, session_id, run_id, project)
    (project / "a.py").write_text("x = 1\n", encoding="utf-8")
    session = _new_session(project)
    base = rev_store.current_state(str(project), ignored_paths=())
    evidence = ExecutionEvidence(
        workspace_revision=int(base.revision or 0),
        workspace_fingerprint=base.fingerprint,
    )
    counts = {"edit": 0, "run": 0}

    def fake_edit(call):
        counts["edit"] += 1
        (project / str(call.args.get("path") or "a.py")).write_text(
            str(call.args.get("content") or "x = 1\n"), encoding="utf-8"
        )
        return ToolResult(ok=True, call=call, model_text="edited", audit={"changed": True})

    def fake_run(call):
        counts["run"] += 1
        # call_id preserves the native id (n{i} style keeps ordering stable).
        idx = counts["run"] - 1
        audit = dict(run_audits[idx]) if idx < len(run_audits) else {"exit_code": 0}
        if forged_run_audit is not None and counts["run"] == 1:
            audit = dict(forged_run_audit)
        return ToolResult(ok=True, call=call, model_text="run out", audit=audit)

    calls = [ToolCall(name="edit", args={"path": "a.py", "content": "x = 2\n"})]
    for i in range(len(run_audits)):
        calls.append(
            ToolCall(name="run", args={"command": "python -m pytest", "path": "."}, call_id=f"native-{run_id}-{i}")
        )
    executors = {"edit": fake_edit, "run": fake_run}
    snapshot = build_turn_snapshot(session)
    results = execute_turn(
        session, calls,
        executors=executors,
        run_id=run_id, effect_scope="task", turn=1,
        project_path=project, intent_sink=sink, snapshot=snapshot,
        execution_evidence=evidence, workspace_revision_store=rev_store,
    )
    assert results and len(results) == len(calls)
    return session, evidence, counts, rev_store


def _recover_formal(project, state, logdir, session_id, run_id):
    """Reopen stores (restart) then recover via the formal entry path."""
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
    # Formal entry creates the session workspace from the durable store
    # before replaying facts (mirrors start_task_session via evidence).
    cur = deps.workspace_revisions.current_state(str(project), ignored_paths=())
    fresh.set_workspace_state(cur.revision, cur.fingerprint)
    frame = SimpleNamespace(run_id=run_id, recovered_tool_outcomes=tuple(recovery.recovered_tool_outcomes),
                            settled_tool_outcomes=recovery.settled_tool_outcomes)
    delivered, rows, resume_start, initial = restore_task_session(frame, fresh)
    return fresh, recovery, delivered, rows, resume_start


def _gate_context(session, run_id="r-x"):
    from codey.runtime.observe.execution_evidence import ExecutionEvidence

    return {
        "execution_evidence": ExecutionEvidence(
            workspace_revision=session.workspace_revision,
            workspace_fingerprint=session.workspace_fingerprint,
        ),
        "project": session.project,
        "scope_files": tuple(sorted(session.edited_files or {})),
        "task_changed": bool(session.edited_files),
        "run_id": run_id,
        "task": "fix a.py",
        "selected_check": getattr(session, "selected_verification", None),
    }


def test_legal_success_recovers_and_completes_via_formal_entry():
    from codey.operations.completion_gate import evaluate

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        project, state, logdir = _dirs(tmp)
        session, _, counts, rev_store = _settle_edit_then_runs(
            project, state, logdir, "s-legal", "r-legal", [{"exit_code": 0}],
        )
        assert counts["edit"] == 1 and counts["run"] == 1
        orig_rev = int(session.workspace_revision or 0)
        orig_fp = str(session.workspace_fingerprint or "")
        assert orig_rev and orig_fp
        orig_call = "native-r-legal-0"

        fresh, recovery, _, rows, resume = _recover_formal(project, state, logdir, "s-legal", "r-legal")
        # No file rewrite during recovery and no extra execution.
        assert counts["edit"] == 1 and counts["run"] == 1
        assert len(fresh.verifications) == 1
        row = fresh.verifications[0]
        assert row["passed"] is True and row["exit_code"] == 0
        assert type(row.get("workspace_revision")) is int
        assert type(row.get("workspace_fingerprint")) is str
        assert fresh.workspace_revision == orig_rev
        assert fresh.workspace_fingerprint == orig_fp
        assert recovery.recovered_tool_outcomes[1].call.call_id == orig_call
        assert resume == max(int(r.turn) for r in rows) + 1
        # Formal gate must complete on the recovered facts.
        assert evaluate(fresh, "done", context=_gate_context(fresh, "r-legal")).complete is True


def test_unknown_exit_recovers_and_blocks():
    from codey.operations.completion_gate import evaluate

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        project, state, logdir = _dirs(tmp)
        _settle_edit_then_runs(
            project, state, logdir, "s-unk", "r-unk", [{"exit_code": 0}, {}],
        )
        fresh, _, _, _, _ = _recover_formal(project, state, logdir, "s-unk", "r-unk")
        assert len(fresh.verifications) == 2
        assert fresh.verifications[-1]["passed"] is False
        assert fresh.verifications[-1].get("exit_code") is None
        assert evaluate(fresh, "done", context=_gate_context(fresh, "r-unk")).complete is False
        assert evaluate(fresh, "done", context=None).complete is False


def test_file_change_after_verification_blocks_after_recovery():
    from codey.operations.completion_gate import evaluate

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        project, state, logdir = _dirs(tmp)
        _settle_edit_then_runs(
            project, state, logdir, "s-change", "r-change", [{"exit_code": 0}],
        )
        fresh, _, _, _, _ = _recover_formal(project, state, logdir, "s-change", "r-change")
        assert evaluate(fresh, "done", context=_gate_context(fresh, "r-change")).complete is True
        (project / "a.py").write_text("x = 3\n", encoding="utf-8")
        assert evaluate(fresh, "done", context=_gate_context(fresh, "r-change")).complete is False


def test_forged_executor_identity_never_becomes_trusted():
    from codey.operations.completion_gate import evaluate

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        project, state, logdir = _dirs(tmp)
        session, _, _, _ = _settle_edit_then_runs(
            project, state, logdir, "s-forge", "r-forge", [{"exit_code": 0}],
            forged_run_audit={
                "exit_code": 0,
                "workspace_revision": True,
                "workspace_fingerprint": "forged",
            },
        )
        # Executor-forged keys are stripped at the kernel boundary.
        assert session.verifications[0].get("workspace_revision") is not True
        fresh, _, _, _, _ = _recover_formal(project, state, logdir, "s-forge", "r-forge")
        assert evaluate(fresh, "done", context=_gate_context(fresh, "r-forge")).complete is True
        assert fresh.verifications == session.verifications
        # The forged True revision must never appear as a trusted pass.
        for row in fresh.verifications:
            assert row.get("workspace_revision") is not True
        # The real kernel identity survives; executor metadata contributes
        # no authority and cannot replace the genuine pair.
        assert all(type(r.get("workspace_revision")) is not bool for r in fresh.verifications)


def test_native_call_id_survives_and_no_reexecution_on_second_restart():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        project, state, logdir = _dirs(tmp)
        _, _, counts, _ = _settle_edit_then_runs(
            project, state, logdir, "s-native", "r-native", [{"exit_code": 0}],
        )
        fresh1, rec1, _, rows1, resume1 = _recover_formal(project, state, logdir, "s-native", "r-native")
        assert rec1.recovered_tool_outcomes[1].call.call_id == "native-r-native-0"
        n1 = len(fresh1.verifications)
        assert n1 == 1
        # Second restart is idempotent: no duplicate replay, no re-execution.
        fresh2, rec2, _, rows2, _ = _recover_formal(project, state, logdir, "s-native", "r-native")
        assert counts["edit"] == 1 and counts["run"] == 1
        assert len(rec2.recovered_tool_outcomes) == len(rec1.recovered_tool_outcomes)
        assert fresh2.verifications == fresh1.verifications
        from codey.operations.completion_gate import evaluate

        c1a = evaluate(fresh1, "done", context=_gate_context(fresh1, "r-native")).complete
        c1b = evaluate(fresh2, "done", context=_gate_context(fresh2, "r-native")).complete
        assert c1a == c1b is True


def test_multi_turn_recovery_keeps_more_than_twenty_observations(monkeypatch):
    """25 runs in legal batches, acknowledged through the recorded provider."""
    import json

    from codey.operations.task_effects import KernelRecordedProvider
    from codey.operations.task_loop import run_task_kernel
    from codey.runtime.core.models import ToolResult

    monkeypatch.setenv("NATIVE_TOOLS", "0")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        project, state, logdir = _dirs(Path(td))
        (project / "a.py").write_text("x = 1\n", encoding="utf-8")
        _, _, _, store, _, sink = _open_runtime(logdir, state, "s-many", "r-many", project)
        session = _new_session(project)
        base = store.current_state(str(project), ignored_paths=())
        session.set_workspace_state(base.revision, base.fingerprint)
        replies = [json.dumps({"tool": "edit", "args": {"path": "a.py", "content": "x = 2\n"}})]
        for start in range(0, 25, 7):
            replies.append("\n".join(
                json.dumps({"tool": "run", "args": {"command": f"python -m pytest test_{i}.py", "path": "."}})
                for i in range(start, min(25, start + 7))
            ))
        replies.append(json.dumps({"tool": "done", "args": {"summary": "finished"}}))
        answers = iter(replies)
        counts = {"edit": 0, "run": 0}
        class WebMulti:
            def new_chat(self):
                pass
            def send(self, text):
                return next(answers)
            def close(self):
                pass
        def do_edit(call):
            counts["edit"] += 1
            (project / call.args["path"]).write_text(call.args["content"], encoding="utf-8")
            return ToolResult(ok=True, call=call, model_text="edited", audit={"changed": True})
        def do_run(call):
            counts["run"] += 1
            return ToolResult(ok=True, call=call, model_text="ok", audit={"exit_code": 0})
        provider = WebMulti()
        try:
            result = run_task_kernel(
                session,
                request=KernelRunRequest(
                    transport=KernelTransportDeps(
                        provider=KernelRecordedProvider(provider, sink),
                        provider_id="local",
                        run_id="r-many",
                        user_task="fix a.py",
                    ),
                    execution=KernelExecutionDeps(
                        executors={"edit": do_edit, "run": do_run},
                        project_path=project,
                        workspace_revision_store=store,
                    ),
                    observation=KernelObservationDeps(
                        intent_sink=sink,
                        completion_context={"run_id": "r-many", "project": str(project)},
                    ),
                ),
            )
            assert result.completed is True
            assert counts == {"edit": 1, "run": 25}
            for _ in range(2):
                fresh, recovery, _, _, _ = _recover_formal(project, state, logdir, "s-many", "r-many")
                assert len(fresh.verifications) == 25
                assert fresh.verifications == session.verifications
                assert fresh.edited_files == session.edited_files
                assert recovery.recovered_tool_outcomes == ()
        finally:
            provider.close()
