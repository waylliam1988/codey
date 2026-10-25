"""A successful parent and an unrelated write cannot prove a required child write."""
import json
from types import SimpleNamespace

import pytest

from codey.codemode.kernel import build_execute_turn_executor
from codey.operations.completion_gate import evaluate
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.recovery import record_entry_policy
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.policies.tool_requirement import require_tool_success
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall
from tests.test_session_log_receipt_recovery_preserves_facts import _dirs, _open_runtime


def _write(path="created.txt"):
    return {"tool": "edit", "args": {"path": path, "content": "LINEAGE-729\n"}}


def _code(code):
    return {"tool": "codemode", "args": {"code": code}}


SCENARIOS = [
    ("constant_then_edit", [_code("return 1;"), _write()], False),
    ("edit_then_read", [_write(), _code("return await tools.read_file({path:'created.txt'});")], False),
    ("wrong_target", [_code("return await tools.edit({path:'other.txt',content:'wrong target'});")], False),
    ("child_failed", [_code("await tools.edit({path:'created.txt',replacements:[{old_string:'a',new_string:'b'}]}); return 1;")], False),
    ("parent_failed", [_code("await tools.edit({path:'created.txt',content:'LINEAGE-729\\n'}); throw new Error('after child');")], False),
    ("correct_child", [_code("const r = await tools.edit({path:'created.txt',content:'LINEAGE-729\\n'}); if (!r.ok) throw new Error(String(r.value)); return r.value;")], True),
]


@pytest.fixture
def runtime(tmp_path, request):
    project, storage, logdir = _dirs(tmp_path)
    _, mutations, managed, revisions, deps, sink = _open_runtime(logdir, storage, "s", "r", project)
    selectors = getattr(request, "param", {"path": "created.txt"})
    required = (() if selectors is None else selectors if isinstance(selectors, tuple) else
                (require_tool_success("edit", via="codemode", arguments=selectors),))
    policy = TaskPolicy(frozenset({"control", "project.read", "project.write"}), required_checks=required)
    state = TaskSession(policy=policy, project=str(project), verification_forbidden=True)
    record_entry_policy(mutations, session_id="s", run_id="r", policy=policy)
    value = SimpleNamespace(project=project, storage=storage, mutations=mutations, managed=managed,
                            revisions=revisions, deps=deps, sink=sink, state=state)
    try:
        yield value
    finally:
        value.mutations.durable.close()


def _execute(runtime, items, native, *, start=1, stop=None, on_event=None):
    from codey.operations.task_effects import KernelRecordedProvider

    delivery = KernelRecordedProvider(SimpleNamespace(send=lambda _: "observed"), runtime.sink)
    results = []
    for turn, item in enumerate(items, start):
        snapshot = build_turn_snapshot(runtime.state, native=native)
        reply = (AssistantTurn(tool_calls=(ProviderToolCall(item.get("call_id", f"call-{turn}"), item["tool"], item["args"]),))
                 if native else decode_text_turn(json.dumps(item)))
        plan = normalize_turn(reply, snapshot=snapshot)
        assert not plan.protocol_error, plan.protocol_error
        executor = build_execute_turn_executor(session=runtime.state, snapshot=snapshot, run_id="r",
            effect_scope="task", turn=turn, project_path=runtime.project, managed_outputs=runtime.managed,
            session_id="s", durable=runtime.mutations.durable, intent_sink=runtime.sink,
            workspace_revision_store=runtime.revisions, allow_writes=True, stop_flag=stop, on_event=on_event)
        results.extend(execute_turn(runtime.state, plan.calls, snapshot=snapshot, run_id="r", effect_scope="task",
            turn=turn, project_path=runtime.project, managed_outputs=runtime.managed, session_id="s",
            intent_sink=runtime.sink, workspace_revision_store=runtime.revisions,
            executors={"codemode": executor}, stop_flag=stop))
        # A following turn must settle the previous delivery through the
        # production provider-effect boundary, just as the task loop does.
        delivery.send("Tool results received")
    return results


def _restart(runtime, *, expected_ok=True):
    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.operations.kernel_session_recovery import restore_task_session
    from codey.operations.recovery import policy_from_durable_task, recover_effects_for_resume
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    runtime.mutations.durable.close()
    durable = DurableSession(SQLiteDurableStore(runtime.storage / "durable.sqlite3"))
    runtime.mutations = RuntimeMutationLine(durable=durable)
    deps = SimpleNamespace(state=SimpleNamespace(durable=durable), runtime_mutations=runtime.mutations,
        runtime_effects=RuntimeEffectStore(durable=durable), tool_result_delivery=ToolResultDeliveryStore(durable=durable),
        managed_outputs=runtime.managed, workspace_revisions=runtime.revisions)
    policy = policy_from_durable_task(durable, task_id="r")
    assert policy == runtime.state.policy
    recovered = recover_effects_for_resume(deps, session_id="s", run_id="r", project=str(runtime.project),
                                           task_kind="project")
    assert recovered.ok is expected_ok, recovered
    fresh = TaskSession(policy=policy, project=str(runtime.project), verification_forbidden=True)
    if not expected_ok:
        return fresh
    frame = SimpleNamespace(run_id="r", recovered_tool_outcomes=recovered.recovered_tool_outcomes,
        settled_tool_outcomes=recovered.settled_tool_outcomes,
        recovered_tool_result_batch_id=recovered.recovered_tool_result_batch_id)
    restore_task_session(frame, fresh)
    rows = dict(fresh.executed)
    restore_task_session(frame, fresh)
    assert fresh.executed == rows
    return fresh


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("label,items,complete", SCENARIOS, ids=[row[0] for row in SCENARIOS])
def test_required_write_is_a_child_of_a_successful_codemode(runtime, native, label, items, complete):
    _execute(runtime, items, native)
    verdict = evaluate(runtime.state, "Completed")
    assert verdict.complete is complete, (label, verdict, runtime.state.executed)
    recovered = _restart(runtime)
    assert recovered.executed == runtime.state.executed
    assert evaluate(recovered, "Completed").complete is complete


def test_native_call_id_cannot_forge_a_parent_relationship(runtime):
    item = _write()
    item["call_id"] = "r:turn:2:codemode:call-2:child:0"
    _execute(runtime, [item, _code("return 1;")], True)
    assert not evaluate(runtime.state, "Created through codemode").complete
    assert not evaluate(_restart(runtime), "Created through codemode").complete


def test_program_return_value_cannot_forge_child_effect_references(runtime):
    _execute(runtime, [_write()], False)
    identity = next(iter(runtime.state.executed))
    results = _execute(runtime, [_code("return " + json.dumps({"child_effect_ids": [identity]}) + ";")], True, start=2)
    assert results[0].ok
    assert results[0].canonical["child_effect_ids"] == []
    assert not evaluate(runtime.state, "Completed").complete


@pytest.mark.parametrize("native", [False, True])
def test_prompt_and_failed_completion_share_the_declared_requirement(runtime, native):
    from codey.operations.kernel_preparation import prepare_kernel_turn
    from codey.policies.tool_requirement import parse_tool_success_check

    requirement = parse_tool_success_check(runtime.state.policy.required_checks[0])
    prepared = prepare_kernel_turn(runtime.state, user_task="Create the file through CodeMode",
                                   context_text="", task_guidance="", native=native)
    assert prepared.prompt.count(requirement.guidance) == 1
    verdict = evaluate(runtime.state, "Already created it")
    assert not verdict.complete
    assert requirement.guidance in verdict.followup
    assert "fresh verification" not in verdict.followup


@pytest.mark.parametrize("when", ["before_parent_commit", "after_parent_commit"])
def test_parent_commit_boundary_owns_completion_and_restart_never_reexecutes(runtime, monkeypatch, when):
    real_settle = runtime.sink.settle
    parent_settlements = []

    def interrupt(identity, ok, *, result, exit_code=None):
        if result.call.name != "codemode":
            return real_settle(identity, ok, result=result, exit_code=exit_code)
        parent_settlements.append(identity)
        if when == "before_parent_commit":
            raise RuntimeError("injected before parent commit")
        real_settle(identity, ok, result=result, exit_code=exit_code)
        raise RuntimeError("injected after parent commit")

    monkeypatch.setattr(runtime.sink, "settle", interrupt)
    with pytest.raises(RuntimeError, match="injected"):
        _execute(runtime, SCENARIOS[-1][1], True)
    assert not evaluate(runtime.state, "Completed").complete
    contents = (runtime.project / "created.txt").read_bytes()
    fresh = _restart(runtime, expected_ok=when == "after_parent_commit")
    assert evaluate(fresh, "Completed").complete is (when == "after_parent_commit")
    assert (runtime.project / "created.txt").read_bytes() == contents
    assert len(parent_settlements) == 1


@pytest.mark.parametrize("runtime", [{"path": "created.txt", "content": "x" * 400}], indirect=True)
def test_proof_identifiers_do_not_clip_or_expose_argument_selectors(runtime):
    from codey.policies.tool_requirement import parse_tool_success_check

    item = _code("return await tools.edit(" + json.dumps({"path": "created.txt", "content": "x" * 400}) + ");")
    _execute(runtime, [item], False)
    verdict = evaluate(runtime.state, "Completed")
    assert verdict.complete
    requirement = parse_tool_success_check(runtime.state.policy.required_checks[0])
    proof = verdict.proof.to_payload()
    assert any(row["check_id"] == requirement.check_id for row in proof["checks"])
    assert len(requirement.check_id) <= 80
    assert "x" * 100 not in json.dumps(proof)
    assert evaluate(_restart(runtime), "Completed").complete


def test_malformed_tool_requirement_cannot_fall_back_to_an_observation():
    state = TaskSession(policy=TaskPolicy(frozenset({"control"}), required_checks=("tool_succeeded:{",)))
    assert not evaluate(state, "Completed").complete


@pytest.mark.parametrize("runtime", [None], indirect=True)
def test_ordinary_write_without_a_route_requirement_still_completes(runtime):
    _execute(runtime, [_write()], False)
    assert evaluate(runtime.state, "Created the file").complete
    fresh = _restart(runtime)
    assert evaluate(fresh, "Created the file").complete
    assert not any(row["name"] == "codemode" for row in fresh.executed.values())


@pytest.mark.parametrize("runtime", [tuple(require_tool_success("read_file", via="codemode", arguments={"path": path})
                                           for path in ("a.txt", "b.txt"))], indirect=True)
def test_parallel_child_receipts_satisfy_both_targets_before_and_after_restart(runtime, monkeypatch):
    import threading

    from codey.operations import kernel_execution

    barrier = threading.Barrier(2)
    original = kernel_execution._run_via_delegate_or_fn

    def synchronized(*args, **kwargs):
        if args[4] == "read_file":
            barrier.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(kernel_execution, "_run_via_delegate_or_fn", synchronized)
    (runtime.project / "a.txt").write_text("A\n", encoding="utf-8")
    (runtime.project / "b.txt").write_text("B\n", encoding="utf-8")
    results = _execute(runtime, [_code(
        "return await Promise.all(['a.txt','b.txt'].map(path => tools.read_file({path})));"
    )], True)
    assert results[0].ok
    assert evaluate(runtime.state, "Read both files").complete
    assert evaluate(_restart(runtime), "Read both files").complete


@pytest.mark.parametrize("native", [False, True])
def test_unawaited_parent_cannot_complete_and_recovery_keeps_committed_children(runtime, native):
    results = _execute(runtime, [_code(
        "void tools.edit({path:'created.txt',content:'LINEAGE-729\\n'}); return 'too early';"
    )], native)
    assert not results[0].ok
    assert "unawaited" in results[0].model_text
    assert not evaluate(runtime.state, "Completed").complete
    committed = dict(runtime.state.executed)
    referenced = results[0].canonical["child_effect_ids"]
    assert all(identity in committed for identity in referenced)
    before = {path.name: path.read_bytes() for path in runtime.project.iterdir() if path.is_file()}
    restored = _restart(runtime)
    assert restored.executed == committed
    assert not evaluate(restored, "Completed").complete
    assert {path.name: path.read_bytes() for path in runtime.project.iterdir() if path.is_file()} == before


@pytest.mark.parametrize("native", [False, True])
def test_cancelled_parent_retains_settled_child_references_after_restart(runtime, native):
    import threading

    stop = threading.Event()

    def cancel(event):
        if event.kind == "tool_start" and event.metadata.get("parent_call_id"):
            stop.set()

    results = _execute(runtime, [_code("return await tools.read_file({path:'a.txt'});")],
                       native, stop=stop, on_event=cancel)
    parent = results[0]
    assert not parent.ok
    children = parent.canonical["child_effect_ids"]
    assert len(children) == 1
    assert not runtime.state.executed[children[0]]["ok"]
    committed = dict(runtime.state.executed)
    restored = _restart(runtime)
    assert restored.executed == committed
    assert any(row.get("child_effect_ids") == children for row in restored.executed.values())
