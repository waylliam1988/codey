"""Real log+receipt recovery preserves verification facts without truncation.

Covers via production RuntimeSessionLog + receipt + single fact replay:
legal success, illegal identity never becomes valid, unknown blocks,
>20 observations are kept, native call ids survive.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace


def _new_log(tmp: Path):
    from codey.runtime.log.session_log import RuntimeSessionLog
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    log = RuntimeSessionLog(tmp / "sesslog")
    mutations = RuntimeMutationLine(log)
    return log, mutations


def _settle_runs(tmp: Path, session_id: str, run_id: str, calls, audits=()):
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.task_effects import KernelEffectSink
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolResult
    from codey.storage.managed_outputs import ManagedOutputStore

    log, mutations = _new_log(tmp)
    mutations.accept_operation(
        session_id=session_id, run_id=run_id, project=str(tmp),
        provider_id="local", turn_budget=20, max_repair_rounds=1, task_kind="project",
    )
    mutations.mark_writer_running(session_id, run_id, provider_id="local")
    sink = KernelEffectSink(
        mutations, session_id=session_id, run_id=run_id, provider_id="local",
        managed_outputs=ManagedOutputStore(tmp / "state"),
    )
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
        project=str(tmp),
    )
    from codey.workspace.revision import workspace_fingerprint

    (tmp / "a.py").write_text("x = 1\n", encoding="utf-8")
    session.record_edit("a.py", revision=1)
    session.set_workspace_state(7, workspace_fingerprint(str(tmp)))
    snapshot = build_turn_snapshot(session)
    audit_by_id = {c.call_id: a for c, a in zip(calls, audits)} if audits else {}

    def _run_executor(call):
        audit = audit_by_id.get(call.call_id, {"exit_code": 0})
        return ToolResult(call=call, model_text="out", audit=dict(audit))

    results = execute_turn(
        session, calls, run_id=run_id, turn=1, project_path=tmp,
        intent_sink=sink, snapshot=snapshot,
        executors={"run": _run_executor},
    )
    return session, results


def _recover_and_replay(tmp: Path, session_id: str, run_id: str):
    from codey.operations.recovery import recover_effects_for_resume
    from codey.operations.task_entry import _replay_recovered_facts
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
    from codey.runtime.log.session_log import RuntimeSessionLog
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    log = RuntimeSessionLog(tmp / "sesslog")
    mutations = RuntimeMutationLine(log)
    deps = SimpleNamespace(
        runtime_mutations=mutations,
        runtime_effects=RuntimeEffectStore(log),
        tool_result_delivery=ToolResultDeliveryStore(log),
        managed_outputs=__import__("codey.storage.managed_outputs", fromlist=["ManagedOutputStore"]).ManagedOutputStore(tmp / "state"),
    )
    recovery = recover_effects_for_resume(
        deps, session_id=session_id, run_id=run_id, project=str(tmp), task_kind="project",
    )
    assert recovery.ok is True
    from codey.operations.recovery import delivered_from_frame
    from codey.operations.task_session import turn_effect_id

    frame = SimpleNamespace(
        run_id=run_id, recovered_tool_outcomes=tuple(recovery.recovered_tool_outcomes),
    )
    delivered = delivered_from_frame(frame, effect_scope="task")
    fresh = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
        project=str(tmp),
    )
    from codey.workspace.revision import workspace_fingerprint

    (tmp / "a.py").write_text("x = 1\n", encoding="utf-8")
    fresh.record_edit("a.py", revision=1)
    fresh.set_workspace_state(7, workspace_fingerprint(str(tmp)))
    # Replay via the single fact entry using recovered results.
    from codey.operations.kernel_facts import record_facts_for_result
    from codey.operations.kernel_recovery_result import frame_outcome_exit_code, frame_outcome_ok

    rows = sorted(recovery.recovered_tool_outcomes, key=lambda r: (r.turn, r.tool_index))
    for row in rows:
        ident = turn_effect_id(f"{run_id}:task", int(row.turn), int(row.tool_index))
        prior = delivered[ident]
        record_facts_for_result(fresh, row.call, prior, ok=frame_outcome_ok(row), exit_code=frame_outcome_exit_code(row))
    return fresh, recovery


def test_legal_success_replays_via_real_log():
    from codey.runtime.core.models import ToolCall

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        calls = [ToolCall("run", {"command": "python -m pytest", "path": "."}, "native-1")]
        _settle_runs(tmp, "s-legal", "r-legal", calls, audits=[{"exit_code": 0}])
        fresh, _ = _recover_and_replay(tmp, "s-legal", "r-legal")
        assert len(fresh.verifications) == 1
        assert fresh.verifications[0]["passed"] is True
        assert fresh.verifications[0]["exit_code"] == 0


def test_unknown_result_replays_as_blocking_observation():
    from codey.runtime.core.models import ToolCall

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        calls = [
            ToolCall("run", {"command": "python -m pytest", "path": "."}, "n1"),
            ToolCall("run", {"command": "python -m pytest", "path": "."}, "n2"),
        ]
        _settle_runs(tmp, "s-unk", "r-unk", calls, audits=[{"exit_code": 0}, {}])
        fresh, _ = _recover_and_replay(tmp, "s-unk", "r-unk")
        assert len(fresh.verifications) == 2
        assert fresh.verifications[-1]["passed"] is False
        assert "exit_code" not in fresh.verifications[-1]
        from codey.operations.completion_gate import evaluate

        assert evaluate(fresh, "done", context=None).complete is False


def test_over_twenty_observations_are_kept():
    from codey.runtime.core.models import ToolCall

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        calls = [
            ToolCall("run", {"command": "python -m pytest", "path": "."}, f"n{i}")
            for i in range(25)
        ]
        _settle_runs(tmp, "s-many", "r-many", calls, audits=[{"exit_code": 0}] * 25)
        fresh, _ = _recover_and_replay(tmp, "s-many", "r-many")
        assert len(fresh.verifications) == 25


def test_native_call_id_survives_real_log():
    from codey.runtime.core.models import ToolCall

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        calls = [ToolCall("run", {"command": "python -m pytest", "path": "."}, "native-keep-1")]
        _settle_runs(tmp, "s-native", "r-native", calls, audits=[{"exit_code": 0}])
        _fresh, recovery = _recover_and_replay(tmp, "s-native", "r-native")
        assert recovery.recovered_tool_outcomes[0].call.call_id == "native-keep-1"


def test_illegal_identity_never_becomes_valid_via_real_log():
    from codey.operations.completion_gate import evaluate
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.workspace.revision import workspace_fingerprint

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        (tmp / "a.py").write_text("x = 1\n", encoding="utf-8")
        fp = workspace_fingerprint(str(tmp))
        session = TaskSession(
            policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
            project=str(tmp),
        )
        session.record_edit("a.py", revision=1)
        session.set_workspace_state(7, fp)
        # Illegal revision type directly in the observation never passes,
        # even without a payload wash.
        session.verifications.append({
            "command": "python -m pytest", "cwd": ".", "revision": 1,
            "passed": True, "exit_code": 0,
            "workspace_revision": True, "workspace_fingerprint": fp,
        })
        assert evaluate(session, "done", context=None).complete is False
