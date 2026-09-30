"""Redelivered verification must never acquire the resumed workspace version."""

import pytest

from codey.operations.completion_gate import evaluate
from codey.operations.kernel_facts import record_facts_for_result
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.runtime.core.models import ToolCall, ToolResult
from codey.workspace.revision import workspace_fingerprint


def test_recovered_run_without_verified_provenance_cannot_prove_current_files(tmp_path):
    (tmp_path / "app.py").write_text("changed after verification\n")
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "project.write", "project.verify"})),
                          task_kind="project", project=str(tmp_path))
    session.set_workspace_state(2, workspace_fingerprint(tmp_path))
    session.record_edit("app.py", revision=1)
    call = ToolCall("run", {"command": "pytest", "path": "."})
    result = ToolResult(call, "original pass", audit={"exit_code": 0})
    record_facts_for_result(session, call, result, ok=True, exit_code=0)
    assert not evaluate(session, "done", context={"run_id": "resumed"}).complete


def test_done_rechecks_files_changed_outside_the_tool_loop(tmp_path):
    file = tmp_path / "app.py"
    file.write_text("verified version\n")
    fingerprint = workspace_fingerprint(tmp_path)
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})), project=str(tmp_path))
    session.set_workspace_state(1, fingerprint)
    session.record_edit("app.py", revision=1)
    session.record_verification("pytest", 1, True, exit_code=0,
                                workspace_revision=1, workspace_fingerprint=fingerprint)
    file.write_text("changed since verification\n")
    assert not evaluate(session, "done", context={"run_id": "changed"}).complete


def test_failed_run_with_exit_zero_cannot_record_passing_verification():
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})))
    call = ToolCall("run", {"command":"pytest", "path":"."})
    record_facts_for_result(session, call, ToolResult(call, "ERROR: execution failed", audit={"exit_code":0}),
                            ok=False, exit_code=0)
    assert session.verifications[-1]["passed"] is False


def test_ignored_generated_files_do_not_invalidate_verification(tmp_path):
    (tmp_path / "app.py").write_text("verified\n")
    generated = tmp_path / "generated"
    generated.mkdir()
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})), project=str(tmp_path))
    fingerprint = workspace_fingerprint(tmp_path, ignored_paths=("generated",))
    session.set_workspace_state(1, fingerprint)
    session.record_edit("app.py", revision=1)
    session.record_verification("pytest", 1, True, exit_code=0,
                                workspace_revision=1, workspace_fingerprint=fingerprint)
    (generated / "output.txt").write_text("new test output")
    assert evaluate(session, "done", context={"run_id":"ignored", "workspace_ignored_paths":("generated",)}).complete


@pytest.mark.parametrize("changed", [False, True])
def test_production_recovery_restores_version_proof_only_for_same_workspace(tmp_path, changed):
    from types import SimpleNamespace

    from codey.operations.kernel_provenance import _with_trusted_workspace_state
    from codey.operations.recovery import delivered_from_frame, recover_effects_for_resume
    from codey.operations.task_effects import KernelEffectSink
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
    from codey.storage.managed_outputs import ManagedOutputStore
    from codey.workspace.revision import WorkspaceRevisionStore
    from tests.test_settled_delivery_recovery import _accept_and_mark, _new_log

    project = tmp_path / "project"
    project.mkdir()
    file = project / "app.py"
    file.write_text("verified version\n")
    revisions = WorkspaceRevisionStore(tmp_path / "workspace")
    version = revisions.current_state(project)
    log, mutations = _new_log(tmp_path)
    _accept_and_mark(mutations, "s", "r", str(project))
    outputs = ManagedOutputStore(tmp_path / "outputs")
    call = ToolCall("run", {"command":"python -m pytest", "path":"."}, "original-call")
    result = _with_trusted_workspace_state(ToolResult(call, "original pass", audit={"exit_code":0}),
                                           revision=version.revision, fingerprint=version.fingerprint)
    sink = KernelEffectSink(mutations, session_id="s", run_id="r", provider_id="local", managed_outputs=outputs)
    sink.begin_turn([("run-effect", call, 0)], turn=1)
    sink.settle("run-effect", True, result=result, exit_code=0)
    if changed:
        file.write_text("changed after verification\n")
    log, mutations = _new_log(tmp_path)
    recovery = recover_effects_for_resume(SimpleNamespace(
        runtime_mutations=mutations, runtime_effects=RuntimeEffectStore(log),
        tool_result_delivery=ToolResultDeliveryStore(log), managed_outputs=outputs,
        workspace_revisions=revisions,
    ), session_id="s", run_id="r", project=str(project), task_kind="project")
    assert recovery.ok
    restored = list(delivered_from_frame(SimpleNamespace(run_id="r", recovered_tool_outcomes=recovery.recovered_tool_outcomes),
                                         effect_scope="task").values())[0]
    assert restored.model_text == "original pass"
    assert restored.call.call_id == "original-call"
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})), project=str(project))
    current = revisions.current_state(project)
    session.set_workspace_state(current.revision, current.fingerprint)
    session.record_edit("app.py", revision=1)
    record_facts_for_result(session, restored.call, restored, ok=True, exit_code=0)
    assert evaluate(session, "done", context={"run_id":"resumed"}).complete is (not changed)


def test_explicit_run_executor_receives_kernel_workspace_proof(tmp_path):
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.kernel_provenance import _kernel_workspace_identity_of

    (tmp_path / "app.py").write_text("verified\n")
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "project.verify"})),
                          project=str(tmp_path))
    session.set_workspace_state(1, workspace_fingerprint(tmp_path))
    call = ToolCall("run", {"command": "python -m pytest", "path": "."})
    results = execute_turn(session, [call], project_path=tmp_path,
                           executors={"run": lambda call: ToolResult(call, "passed", audit={"exit_code": 0})},
                           snapshot=build_turn_snapshot(session), run_id="r", turn=1)
    assert _kernel_workspace_identity_of(results[0]).trusted
    assert session.verifications[-1]["workspace_fingerprint"] == session.workspace_fingerprint
