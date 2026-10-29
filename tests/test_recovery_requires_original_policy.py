"""Losing authorization also loses completion requirements: recovery must block."""
import pytest

from codey.operations.kernel_errors import RecoveryFailed
from codey.operations.recovery import policy_from_durable_task, record_entry_policy
from codey.policies.task_policy import TaskPolicy
from codey.runtime.write.mutation_line import RuntimeMutationLine
from tests.test_session_log_receipt_recovery_preserves_facts import _dirs, _open_runtime


def _durable_task(
    session_id: str,
    run_id: str,
    project: object,
    *,
    task_kind: str = "project",
    policy: TaskPolicy | None = None,
    state: dict[str, object] | None = None,
):
    from codey.durable import DurableSession, MemoryDurableStore

    durable = DurableSession(MemoryDurableStore())
    durable.admit_input(
        conversation_id=session_id,
        request_id=run_id,
        task_id=run_id,
        text="recover task",
        provider_snapshot={"provider_id": "local", "model_id": "test"},
        task_state={"task_kind": task_kind, **(state or {})},
    )
    durable.start_task(run_id, state={"task_kind": task_kind, **(state or {})})
    line = RuntimeMutationLine(durable=durable)
    line.accept_operation(session_id=session_id, run_id=run_id, provider_id="local", turn_budget=8, max_repair_rounds=1, task_kind=task_kind)
    if policy is not None:
        durable.record_task_policy(run_id, policy.to_payload())
    return durable


@pytest.mark.parametrize("corruption", ["missing", "version", "required_checks", "strict_research"])
def test_missing_or_invalid_original_policy_cannot_become_weaker_task(tmp_path, corruption):
    project, state, logdir = _dirs(tmp_path)
    log, mutations, *_ = _open_runtime(logdir, state, "s", "r", project)
    original = TaskPolicy(grants=frozenset({"control", "web.read"}), strict_research=True,
                          required_checks=("research_evidence_saved",))
    payload = None
    if corruption != "missing":
        payload = original.to_payload()
        payload[corruption] = {
            "version": 99,
            "required_checks": [False],
            "strict_research": "false",
        }[corruption]
    durable = _durable_task(
        "s", "r", project, state={} if payload is None else {"task_policy": payload}
    )
    with pytest.raises(RecoveryFailed, match="policy"):
        policy_from_durable_task(durable, task_id="r")


def test_valid_original_policy_is_restored_exactly(tmp_path):
    project, state, logdir = _dirs(tmp_path)
    log, mutations, *_ = _open_runtime(logdir, state, "s", "r", project)
    original = TaskPolicy(grants=frozenset({"control", "web.read"}), strict_research=True,
                          required_checks=("research_evidence_saved",))
    durable = _durable_task("s", "r", project, policy=original)
    mutations = RuntimeMutationLine(durable=durable)
    record_entry_policy(mutations, session_id="s", run_id="r", policy=original, durable=durable)
    assert policy_from_durable_task(durable, task_id="r").to_payload() == original.to_payload()


@pytest.mark.parametrize("kind", ["project", "research", "planning"])
def test_every_recovery_dispatch_uses_original_policy(tmp_path, monkeypatch, kind):
    from types import SimpleNamespace

    from codey.operations.context import RunWork
    from codey.operations.result import ModeOutcome
    from codey.operations.task_phases.dispatch import dispatch_run_mode
    from codey.runtime.observe.execution_evidence import ExecutionEvidence
    from codey.task.model import TaskSubmission

    project, state, logdir = _dirs(tmp_path)
    log, mutations, *_ = _open_runtime(logdir, state, "s", "r", project)
    original = TaskPolicy(grants=frozenset({"control", "project.read"}),
                          strict_research=True, required_checks=("research_evidence_saved",))
    durable = _durable_task("s", "r", project, policy=original)
    mutations = RuntimeMutationLine(durable=durable)
    record_entry_policy(mutations, session_id="s", run_id="r", policy=original, durable=durable)
    request = TaskSubmission("s", str(project), "edit a.py and search official web docs",
                             8, False, "web", intent=kind, run_id="r")
    frame = SimpleNamespace(request=request, project_text=str(project), run_id="r", entry_policy=None,
                            recovered_tool_outcomes=(), settled_tool_outcomes=("original fact",))
    seen = []
    def consume(active_frame, *args, **kwargs):
        seen.append(active_frame.entry_policy)
        return ModeOutcome({"stop_reason": "blocked"})
    monkeypatch.setattr("codey.operations.task_phases.dispatch._research_deps", lambda deps: None)
    monkeypatch.setattr("codey.operations.task_phases.dispatch.run_task_mode", consume)
    dispatch_run_mode(SimpleNamespace(runtime_mutations=mutations, state=SimpleNamespace(durable=durable)), None, None,
                      RunWork([], ExecutionEvidence()), frame, SimpleNamespace(), kind, None)
    assert len(seen) == 1
    assert seen[0].to_payload() == original.to_payload()


@pytest.mark.parametrize("field", sorted(TaskPolicy(frozenset({"control"})).to_payload()))
def test_policy_recovery_rejects_missing_schema_fields(field):
    payload = TaskPolicy(frozenset({"control"}), strict_research=True,
                         required_checks=("research_evidence_saved",)).to_payload()
    del payload[field]
    with pytest.raises(ValueError, match="fields"):
        TaskPolicy.from_payload(payload)


@pytest.mark.parametrize("field,value", [("version", True), ("version", "1"),
                                         ("strict_research", "false"),
                                         ("sources_open_required", 1)])
def test_serialization_cannot_launder_invalid_policy_values(field, value):
    from dataclasses import replace

    invalid = replace(TaskPolicy(frozenset({"control"})), **{field: value})
    with pytest.raises(ValueError, match=field):
        TaskPolicy.from_payload(invalid.to_payload())


def test_policy_roundtrip_preserves_denials_without_rewriting_grants():
    original = TaskPolicy(frozenset({"project.read", "project.write"}),
                          denied_capabilities=frozenset({"project.write"}))
    restored = TaskPolicy.from_payload(original.to_payload())
    assert restored == original
    assert not restored.allows("project.write")


@pytest.mark.parametrize("payload", [None, [], "policy"])
def test_nonpolicy_payload_does_not_become_new_minimal_policy(payload):
    with pytest.raises(ValueError):
        TaskPolicy.from_payload(payload)



def test_project_dispatch_records_policy_before_first_tool(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from codey.operations.context import RunWork
    from codey.operations.result import ModeOutcome
    from codey.operations.task_phases.dispatch import dispatch_run_mode
    from codey.runtime.observe.execution_evidence import ExecutionEvidence
    from codey.task.model import TaskSubmission

    project, state, logdir = _dirs(tmp_path)
    log, mutations, *_ = _open_runtime(logdir, state, "s", "r", project)
    durable = _durable_task("s", "r", project)
    mutations = RuntimeMutationLine(durable=durable)
    request = TaskSubmission("s", str(project), "edit a.py", 8, False, "local", intent="project")
    frame = SimpleNamespace(request=request, project_text=str(project), run_id="r", entry_policy=None,
                            recovered_tool_outcomes=(), settled_tool_outcomes=())
    seen = []
    def consume(active_frame, *args, **kwargs):
        assert policy_from_durable_task(durable, task_id="r") == active_frame.entry_policy
        seen.append(True)
        return ModeOutcome({"stop_reason": "blocked"})
    monkeypatch.setattr("codey.operations.task_phases.dispatch._research_deps", lambda deps: None)
    monkeypatch.setattr("codey.operations.task_phases.dispatch.run_task_mode", consume)
    dispatch_run_mode(SimpleNamespace(runtime_mutations=mutations, state=SimpleNamespace(durable=durable)), None, None,
                      RunWork([], ExecutionEvidence()), frame, None, "project", None)
    assert seen == [True]


def test_original_modification_requirement_survives_changed_resume_request(tmp_path):
    from dataclasses import replace

    from codey.operations.completion_gate import evaluate
    from codey.operations.task_entry import build_task_policy_for_entry, start_task_session
    from tests.test_entry_session_task_requirements_init import _frame, _work

    frame = _frame("edit a.py")
    frame.project_text = str(tmp_path)
    original_request = replace(frame.request, project=str(tmp_path), project_changes_required=True)
    policy = build_task_policy_for_entry(original_request, "project")
    resumed = start_task_session(frame, _work(), TaskPolicy.from_payload(policy.to_payload()), "project")
    assert frame.request.project_changes_required is False
    assert evaluate(resumed, "done").complete is False


def test_original_verification_prohibition_survives_changed_resume_request():
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.task_entry import build_task_policy_for_entry, start_task_session
    from tests.test_entry_session_task_requirements_init import _frame, _work

    original = _frame("edit a.py; do not run tests")
    policy = build_task_policy_for_entry(original.request, "project")
    resumed = _frame("continue task and run pytest")
    session = start_task_session(resumed, _work(), TaskPolicy.from_payload(policy.to_payload()), "project")
    assert session.verification_forbidden is True
    assert "run" not in build_turn_snapshot(session).tool_names



@pytest.mark.parametrize("persisted", [True, False])
def test_resume_before_first_settlement_keeps_original_policy_or_blocks(tmp_path, monkeypatch, persisted):
    from types import SimpleNamespace

    from codey.operations.context import RunWork
    from codey.operations.result import ModeOutcome
    from codey.operations.task_phases.dispatch import dispatch_run_mode
    from codey.runtime.core.operation_state import RuntimeOperationStore
    from codey.runtime.observe.execution_evidence import ExecutionEvidence
    from codey.task.model import TaskSubmission

    project, state, logdir = _dirs(tmp_path)
    log, mutations, *_ = _open_runtime(logdir, state, "s", "r", project)
    original = TaskPolicy(frozenset({"control", "project.read"}), required_checks=("custom_required",))
    # The runtime fixture already owns the admitted durable task. Reuse its
    # session so policy and operation state cannot diverge across stores.
    durable = mutations.durable
    if persisted:
        record_entry_policy(mutations, session_id="s", run_id="r", policy=original, durable=durable)
    mutations.mark_writer_running("s", "r", provider_id="local")
    operation = RuntimeOperationStore(durable=durable).load("s", "r")
    frame = SimpleNamespace(request=TaskSubmission("s", str(project), "edit and search web", 8, False,
                                                   "local", intent="hybrid", run_id="r",
                                                   previous_run_id="r"),
                            project_text=str(project), run_id="r", entry_policy=None, recovered_tool_outcomes=(), settled_tool_outcomes=())
    seen = []
    def consume(active_frame, *args, **kwargs):
        seen.append(active_frame.entry_policy)
        return ModeOutcome({"stop_reason": "blocked"})
    monkeypatch.setattr("codey.operations.task_phases.dispatch.run_task_mode", consume)
    work = RunWork([], ExecutionEvidence(), operation=operation)
    deps = SimpleNamespace(runtime_mutations=mutations, state=SimpleNamespace(durable=durable))
    if persisted:
        dispatch_run_mode(deps, None, None, work, frame, None, "hybrid", None)
        assert seen == [original]
    else:
        with pytest.raises(RecoveryFailed, match="policy"):
            dispatch_run_mode(deps, None, None, work, frame, None, "hybrid", None)
        assert not seen


def test_verification_requirement_parser_failure_cannot_grant_verification(monkeypatch):
    from types import SimpleNamespace

    from codey.operations.task_entry import build_task_policy_for_entry

    def broken(task):
        raise ValueError("cannot classify requirement")
    monkeypatch.setattr("codey.agents.protocol.task_forbids_verification", broken)
    with pytest.raises(ValueError, match="classify"):
        build_task_policy_for_entry(SimpleNamespace(project="project", task="do not run tests"), "project")



@pytest.mark.parametrize("change", ["grant", "drop_requirement", "drop_denial"])
def test_persisted_authorization_snapshot_cannot_be_replaced(tmp_path, change):
    from dataclasses import replace

    from codey.runtime.core.operation_state import RuntimeOperationTransitionError

    project, state, logdir = _dirs(tmp_path)
    log, mutations, *_ = _open_runtime(logdir, state, "s", "r", project)
    original = TaskPolicy(frozenset({"control", "project.read"}), required_checks=("must_check",),
                          denied_capabilities=frozenset({"project.write"}))
    durable = _durable_task("s", "r", project, policy=original)
    mutations = RuntimeMutationLine(durable=durable)
    record_entry_policy(mutations, session_id="s", run_id="r", policy=original, durable=durable)
    before = durable.commit_sequence()
    record_entry_policy(mutations, session_id="s", run_id="r", policy=original, durable=durable)
    assert durable.commit_sequence() == before
    changed = replace(original, **{
        "grant": {"grants": original.grants | {"project.write"}},
        "drop_requirement": {"required_checks": ()},
        "drop_denial": {"denied_capabilities": frozenset()},
    }[change])
    with pytest.raises(RuntimeOperationTransitionError, match="immutable"):
        record_entry_policy(mutations, session_id="s", run_id="r", policy=changed, durable=durable)
    assert durable.commit_sequence() == before
    assert policy_from_durable_task(durable, task_id="r") == original
