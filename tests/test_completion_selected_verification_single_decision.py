"""Unified completion decision: selected verification cannot be replaced.

Same fact must give the same verdict with and without evidence context.
Specifying pytest but only observing compileall success must block in both
paths; observing the specified pytest success must pass in both.
"""
from __future__ import annotations

import pytest

from codey.completion.verification_policy import VerificationCandidate
from codey.operations.completion_gate import evaluate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.observe.execution_evidence import ExecutionEvidence
from codey.workspace.revision import workspace_fingerprint


def make_session(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
        task_kind="project",
        project=str(tmp_path),
        max_turns=3,
    )
    session.record_edit("a.py", revision=1)
    session.set_workspace_state(7, workspace_fingerprint(str(tmp_path)))
    session.selected_verification = VerificationCandidate(
        command="python -m pytest", cwd=".", source="project",
    )
    return session


def _context(session, tmp_path, run_id="selected-check"):
    return {
        "execution_evidence": ExecutionEvidence(
            workspace_revision=session.workspace_revision,
            workspace_fingerprint=session.workspace_fingerprint,
        ),
        "project": str(tmp_path),
        "scope_files": ("a.py",),
        "run_id": run_id,
        "task": "fix a.py",
    }


@pytest.mark.parametrize("with_evidence", [False, True])
@pytest.mark.parametrize(
    "observed, expected",
    [("python -m compileall", False), ("python -m pytest", True)],
)
def test_selected_verification_is_not_replaced(tmp_path, with_evidence, observed, expected):
    session = make_session(tmp_path)
    session.record_verification(
        observed, 1, True, exit_code=0,
        workspace_revision=session.workspace_revision,
        workspace_fingerprint=session.workspace_fingerprint,
        cwd=".",
    )
    context = _context(session, tmp_path) if with_evidence else None
    verdict = evaluate(session, "done", context=context)
    assert verdict.complete is expected


@pytest.mark.parametrize("with_evidence", [False, True])
def test_subdirectory_cwd_is_preserved(tmp_path, with_evidence):
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
        task_kind="project",
        project=str(tmp_path),
        max_turns=3,
    )
    session.record_edit("pkg/a.py", revision=1)
    session.set_workspace_state(9, workspace_fingerprint(str(tmp_path)))
    session.selected_verification = VerificationCandidate(
        command="python -m pytest tests/test_a.py", cwd="pkg", source="project",
    )
    session.record_verification(
        "python -m pytest tests/test_a.py", 1, True, exit_code=0,
        workspace_revision=session.workspace_revision,
        workspace_fingerprint=session.workspace_fingerprint,
        cwd="pkg",
    )
    context = None
    if with_evidence:
        context = {
            "execution_evidence": ExecutionEvidence(
                workspace_revision=session.workspace_revision,
                workspace_fingerprint=session.workspace_fingerprint,
            ),
            "project": str(tmp_path),
            "scope_files": ("pkg/a.py",),
            "run_id": "cwd-check",
            "task": "fix pkg/a.py",
        }
    verdict = evaluate(session, "done", context=context)
    assert verdict.complete is True


@pytest.mark.parametrize("with_evidence", [False, True])
def test_cwd_mismatch_without_coverage_blocks(tmp_path, with_evidence):
    (tmp_path / "pkg").mkdir(exist_ok=True)
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "other").mkdir(exist_ok=True)
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
        task_kind="project",
        project=str(tmp_path),
        max_turns=3,
    )
    session.record_edit("pkg/a.py", revision=1)
    session.set_workspace_state(11, workspace_fingerprint(str(tmp_path)))
    session.selected_verification = VerificationCandidate(
        command="python -m pytest", cwd="pkg", source="project",
    )
    # Same command but sibling cwd: must not cover pkg/a.py.
    session.record_verification(
        "python -m pytest", 1, True, exit_code=0,
        workspace_revision=session.workspace_revision,
        workspace_fingerprint=session.workspace_fingerprint,
        cwd="other",
    )
    context = None
    if with_evidence:
        context = {
            "execution_evidence": ExecutionEvidence(
                workspace_revision=session.workspace_revision,
                workspace_fingerprint=session.workspace_fingerprint,
            ),
            "project": str(tmp_path),
            "scope_files": ("pkg/a.py",),
            "run_id": "cwd-mismatch",
            "task": "fix pkg/a.py",
        }
    verdict = evaluate(session, "done", context=context)
    # Either the selected check is uncovered or the cwd does not cover the
    # scope; both paths must agree to block.
    assert verdict.complete is False


@pytest.mark.parametrize("with_evidence", [False, True])
def test_forbidden_verification_does_not_swallow_required_edit(tmp_path, with_evidence):
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
        task_kind="project",
        project=str(tmp_path),
        max_turns=3,
    )
    session.project_changes_required = True
    session.verification_forbidden = True
    context = None
    if with_evidence:
        context = {
            "execution_evidence": ExecutionEvidence(
                workspace_revision=session.workspace_revision,
                workspace_fingerprint=session.workspace_fingerprint,
            ),
            "project": str(tmp_path),
            "scope_files": (),
            "run_id": "forbidden-no-edit",
            "task": "fix a.py",
        }
    verdict = evaluate(session, "done", context=context)
    assert verdict.complete is False


@pytest.mark.parametrize("with_evidence", [False, True])
def test_forbidden_with_real_edit_defers_to_other_checks(tmp_path, with_evidence):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    session = TaskSession(
        policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
        task_kind="project",
        project=str(tmp_path),
        max_turns=3,
    )
    session.record_edit("a.py", revision=1)
    session.set_workspace_state(13, workspace_fingerprint(str(tmp_path)))
    session.project_changes_required = True
    session.verification_forbidden = True
    context = None
    if with_evidence:
        context = {
            "execution_evidence": ExecutionEvidence(
                workspace_revision=session.workspace_revision,
                workspace_fingerprint=session.workspace_fingerprint,
            ),
            "project": str(tmp_path),
            "scope_files": ("a.py",),
            "run_id": "forbidden-with-edit",
            "task": "fix a.py",
        }
    evaluate(session, "done", context=context)
    # Verification is exempt; the edit requirement is satisfied so the gate
    # must not fail on missing verification (other domains may still block,
    # but the project check itself must not demand a run).
    from codey.operations.project_completion_checks import project_completion_checks

    rows = project_completion_checks(session, context)
    by_id = {r.check_id: r.status for r in rows}
    assert by_id.get("relevant_verification") == "not_applicable"



@pytest.mark.parametrize("field", ["command", "cwd"])
def test_long_verification_identity_is_never_a_display_clip(tmp_path, field):
    from codey.operations.project_completion_checks import _evidence_with_session_facts

    session = make_session(tmp_path)
    command = "python -m pytest" + " -q" * 200
    cwd = "segment/" * 85 + "selected"
    session.record_verification(command, 1, True, exit_code=0, cwd=cwd,
                                workspace_revision=session.workspace_revision,
                                workspace_fingerprint=session.workspace_fingerprint)
    assert session.verifications[-1][field] == {"command": command, "cwd": cwd}[field]
    evidence = _context(session, tmp_path)["execution_evidence"]
    evidence, gaps = _evidence_with_session_facts(evidence, session)
    assert not gaps
    assert evidence.checks_after_edit[-1].command == command
    assert evidence.checks_after_edit[-1].cwd == cwd


def test_long_commands_do_not_merge_failed_and_successful_observations(tmp_path):
    from codey.operations.project_completion_checks import _latest_observations_by_key

    session = make_session(tmp_path)
    prefix = "python -m pytest " + " " * 550
    first, second = prefix + "tests/test_selected.py", prefix + "tests/test_other.py"
    for command, passed in ((first, False), (second, True)):
        session.record_verification(command, 1, passed, exit_code=0 if passed else 1,
                                    workspace_revision=session.workspace_revision,
                                    workspace_fingerprint=session.workspace_fingerprint)
    selected = _latest_observations_by_key(session.verifications, 1)
    assert set(selected) == {(first.strip(), "."), (second.strip(), ".")}
    assert selected[(first.strip(), ".")]["passed"] is False
    assert selected[(second.strip(), ".")]["passed"] is True


def test_event_evidence_keeps_original_long_verification_identity(tmp_path):
    from codey.toolchain.runtime import ToolOutcome

    session = make_session(tmp_path)
    evidence = _context(session, tmp_path)["execution_evidence"]
    command, cwd = "python -m pytest" + " -q" * 200, "segment/" * 85
    evidence._record_run({"command": command, "path": cwd}, ToolOutcome("pass", True, exit_code=0))
    assert evidence.checks_after_edit[-1].command == command
    assert evidence.checks_after_edit[-1].cwd == cwd



def test_real_receipt_restart_preserves_long_command_identity(tmp_path):
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.recovery import record_entry_policy
    from codey.runtime.core.models import ToolCall, ToolResult
    from tests.test_session_log_receipt_recovery_preserves_facts import _dirs, _open_runtime, _recover_formal

    project, state, logdir = _dirs(tmp_path)
    session = make_session(project)
    log, mutations, managed, store, deps, sink = _open_runtime(logdir, state, "s", "r", project)
    current = store.current_state(str(project))
    session.set_workspace_state(current.revision, current.fingerprint)
    record_entry_policy(mutations, session_id="s", run_id="r", policy=session.policy)
    command = "python -m pytest" + " -q" * 200
    result = execute_turn(session, [ToolCall("run", {"command": command, "path": "."})],
                          executors={"run": lambda call: ToolResult(call, "passed", ok=True, audit={"exit_code": 0})},
                          run_id="r", turn=1, effect_scope="task", project_path=project,
                          intent_sink=sink, workspace_revision_store=store)[0]
    assert result.model_text == "passed"
    restored, recovery, delivered, rows, start = _recover_formal(project, state, logdir, "s", "r")
    assert recovery.ok
    assert session.verifications[-1]["command"] == command
    assert restored.verifications[-1]["command"] == command
    assert rows[0].call.args["command"] == command
