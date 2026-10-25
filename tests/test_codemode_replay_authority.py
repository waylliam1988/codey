"""Production replay always re-enters execute_turn and consumes original receipts."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from codey.codemode.kernel import build_execute_turn_executor
from codey.codemode.runtime import NodeWorkerRuntime
from codey.durable import DurableSession, SQLiteDurableStore
from codey.operations.kernel_execution import execute_turn
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.runtime.core.models import ToolCall
from tests.test_session_log_receipt_recovery_preserves_facts import _dirs, _new_session, _open_runtime, _tracker_state


def fixture(tmp_path):
    project, state, logdir = _dirs(tmp_path)
    (project / "a.py").write_text("needle\n")
    _, mutations, managed, revisions, deps, sink = _open_runtime(logdir, state, "s", "r", project)
    session = _new_session(project)
    from codey.operations.recovery import record_entry_policy

    record_entry_policy(mutations, session_id="s", run_id="r", policy=session.policy)
    return project, state, logdir, mutations, managed, revisions, sink, session


def builder(project, mutations, managed, revisions, sink, session, **kwargs):
    return build_execute_turn_executor(
        session=session,
        snapshot=build_turn_snapshot(session),
        run_id="r",
        effect_scope="outer",
        turn=0,
        project_path=project,
        managed_outputs=managed,
        workspace_revision_store=revisions,
        intent_sink=sink,
        durable=mutations.durable,
        session_id="s",
        **kwargs,
    )


def test_completed_parent_restores_original_receipt_without_vm(tmp_path, monkeypatch):
    project, state, logdir, m, managed, revs, sink, session = fixture(tmp_path)
    call = ToolCall("codemode", {"code": "return await tools.read_file({path:'a.py'});"}, call_id="parent")
    parent = builder(project, m, managed, revs, sink, session)
    args = dict(
        executors={"codemode": parent},
        run_id="r",
        effect_scope="outer",
        turn=0,
        snapshot=build_turn_snapshot(session),
        intent_sink=sink,
        project_path=project,
        managed_outputs=managed,
        workspace_revision_store=revs,
        durable=m.durable,
    )
    try:
        first = execute_turn(session, [call], **args)[0]
        assert first.ok, first.model_text
        monkeypatch.setattr(NodeWorkerRuntime, "execute", lambda *a, **k: pytest.fail("restarted VM"))
        second = execute_turn(session, [call], **args)[0]
        assert second.ok, second.model_text
        assert second.model_text == first.model_text
        assert second.canonical == first.canonical
        assert len(second.canonical["child_effect_ids"]) == 1
        state = m.durable.task("r").state
        assert "__execution__" not in json.dumps(dict(state), default=str)
        assert "needle" not in json.dumps(dict(state.get("codemode_replay", {})), default=str)
    finally:
        m.durable.close()


def test_missing_child_receipt_blocks_instead_of_reexecuting(tmp_path):
    from codey.codemode.identity import parent_execution_id
    from codey.operations.task_effects import KernelEffectSink
    from codey.operations.task_session import turn_effect_id

    project, state, logdir, m, managed, revs, sink, session = fixture(tmp_path)
    call = ToolCall(
        "codemode", {"code": "try { await tools.read_file({path:'a.py'}); } catch {} return 1;"}, call_id="parent"
    )
    snapshot = build_turn_snapshot(session)
    specs = {spec.name: spec for spec in snapshot.frozen_specs}
    parent_id = parent_execution_id("r", 0, call)
    parent_effect = turn_effect_id("r:outer", 0, 0)
    child_id = turn_effect_id(f"r:outer:codemode:{parent_id}:child:0", 0, 0)
    child = ToolCall("read_file", {"path": "a.py"}, call_id=f"{parent_id}:child:0")
    # Only the original intent ledger records an attempted child. No shadow
    # journal is needed to recognize an interrupted, unreceipted execution.
    sink.begin_turn([(parent_effect, call, 0)], turn=0, specs=specs, scope="outer")
    sink.for_child(call, turn=0).begin_turn(
        [(child_id, child, 0)], turn=0, specs=specs, scope=f"outer:codemode:{parent_id}:child:0"
    )
    reopened_sink = KernelEffectSink(
        m, session_id="s", run_id="r", provider_id="local", managed_outputs=managed, durable=m.durable
    )
    try:
        parent = builder(project, m, managed, revs, reopened_sink, session)
        result = execute_turn(
            session,
            [call],
            executors={"codemode": parent},
            run_id="r",
            effect_scope="outer",
            turn=0,
            snapshot=snapshot,
            intent_sink=reopened_sink,
            durable=m.durable,
        )[0]
        assert not result.ok
        assert "receipt is missing or uncertain" in result.model_text
        assert m.durable.effect(child_id).settlement is None
        assert len(m.durable.effects("r")) == 2
        assert "children" not in str(m.durable.task("r").state.get("codemode_replay", {}))
    finally:
        m.durable.close()


def test_parallel_result_delivery_and_child_identity_are_deterministic():
    from codey.codemode.host import CodeModeHost
    from codey.codemode.types import ToolCatalogEntry
    from codey.runtime.core.models import ToolResult

    observed = []
    catalog = (ToolCatalogEntry("read_file", "read", parallel_safe=True),)

    def execute(call):
        # Completion order deliberately differs from issuance order.
        time.sleep(0.05 if call.args["path"] == "slow" else 0)
        observed.append((call.call_id, call.args["path"]))
        return ToolResult(ok=True, call=call, model_text=call.args["path"])

    host = CodeModeHost(execution_id="cm", catalog=catalog, execute=execute)
    result = NodeWorkerRuntime().execute(
        "return await Promise.race(['slow','fast'].map(path=>tools.read_file({path})));",
        catalog=catalog,
        invoke=host.invoke,
    )
    assert result.ok
    assert result.value == {"ok": True, "value": "slow"}
    assert set(observed) == {("cm:child:0", "slow"), ("cm:child:1", "fast")}


@pytest.mark.parametrize("read_path", ["a.py", "./a.py"])
@pytest.mark.parametrize("tamper", [False, True, "runtime"])
def test_formal_resume_reconstructs_pending_parent_without_leaf_reexecution(tmp_path, monkeypatch, read_path, tamper):
    from codey.codemode.runtime import NodeWorkerRuntime
    from codey.operations import kernel_execution
    from codey.operations.kernel_session_recovery import restore_task_session
    from codey.operations.recovery import recover_effects_for_resume

    class Crash(BaseException):
        pass

    project, state, logdir, m, managed, revs, sink, session = fixture(tmp_path)
    call = ToolCall(
        "codemode",
        {
            "code": """
        const n = (await load('count')) ?? 0;
        await store('count', n+1);
        const r = await tools.read_file({path:inputs.path});
        return {n,r,text:inputs.text,now:Date.now(),rand:Math.random()};
    """, "inputs": {"path": read_path, "text": "'quoted'\n\"text\"\u2028\n"}
        },
        call_id="parent",
    )
    if tamper is True:
        (project / "a.py").write_text("needle" * 3000)
    original_values = []
    original = NodeWorkerRuntime.execute

    def interrupted(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        assert result.ok
        original_values.append(result.value)
        raise Crash()  # child committed, parent has no receipt

    monkeypatch.setattr(NodeWorkerRuntime, "execute", interrupted)
    parent = builder(project, m, managed, revs, sink, session)
    with pytest.raises(Crash):
        execute_turn(
            session,
            [call],
            executors={"codemode": parent},
            run_id="r",
            effect_scope="outer",
            turn=0,
            snapshot=build_turn_snapshot(session),
            intent_sink=sink,
            project_path=project,
            managed_outputs=managed,
            workspace_revision_store=revs,
            durable=m.durable,
        )
    m.durable.close()
    if tamper is True:
        artifact = max((p for p in managed.root.rglob("*") if p.is_file()), key=lambda p: p.stat().st_size)
        artifact.write_text("corrupt original receipt")

    monkeypatch.setattr(NodeWorkerRuntime, "execute", original)
    if tamper == "runtime":
        monkeypatch.setattr(NodeWorkerRuntime, "contract_digest", lambda _: "changed-worker")
    durable = DurableSession(SQLiteDurableStore(state / "durable.sqlite3"))
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    deps = SimpleNamespace(
        state=_tracker_state(durable, state),
        runtime_mutations=RuntimeMutationLine(durable=durable),
        runtime_effects=RuntimeEffectStore(durable=durable),
        tool_result_delivery=ToolResultDeliveryStore(durable=durable),
        managed_outputs=managed,
        workspace_revisions=revs,
    )

    def no_dispatch(*args, **kwargs):
        if args[4] == "read_file":
            pytest.fail("re-executed committed child")
        return invoke(*args, **kwargs)

    invoke = kernel_execution._run_via_delegate_or_fn
    monkeypatch.setattr(kernel_execution, "_run_via_delegate_or_fn", no_dispatch)
    try:
        recovery = recover_effects_for_resume(
            deps, session_id="s", run_id="r", project=str(project), task_kind="project"
        )
        if tamper:
            assert not recovery.ok
            return
        assert recovery.ok
        assert all(e.settlement is not None for e in durable.effects("r"))
        fresh = _new_session(project)
        frame = SimpleNamespace(
            run_id="r",
            recovered_tool_outcomes=recovery.recovered_tool_outcomes,
            settled_tool_outcomes=recovery.settled_tool_outcomes,
            recovered_tool_result_batch_id=recovery.recovered_tool_result_batch_id,
        )
        _, _, _, initial = restore_task_session(frame, fresh)
        assert len(initial) == 1
        assert initial[0].call.name == "codemode"
        assert len(initial[0].canonical["child_effect_ids"]) == 1
        assert len(fresh.executed) == 2
        assert initial[0].canonical["value"] == original_values[0]
        assert initial[0].canonical["value"]["text"] == call.args["inputs"]["text"]
        assert durable.task("r").state["codemode_state"]["count"] == 1
        assert len(deps.tool_result_delivery.load_batches("s", "r")) == 1
        assert all(
            e.intent.get("parent_effect_id") for e in durable.effects("r") if e.intent["tool_name"] != "codemode"
        )
    finally:
        durable.close()


@pytest.mark.parametrize("ending", ["no_progress", "budget_drain"])
def test_terminal_native_receipt_drain_settles_received_generation_before_writer(tmp_path, monkeypatch, ending):
    from codey.operations.generation_boundary import durable_generation_boundary_callback
    from codey.operations.task_effects import KernelRecordedProvider
    from codey.operations.task_loop import (
        KernelExecutionDeps,
        KernelObservationDeps,
        KernelRunRequest,
        KernelTransportDeps,
        run_task_kernel,
    )
    from codey.providers.base import AssistantTurn, ProviderToolCall

    project, state, logdir, m, managed, revs, sink, session = fixture(tmp_path)
    sink.defer_generation_settlement = True
    session.max_turns = 4 if ending == "no_progress" else 1
    monkeypatch.setenv("NATIVE_TOOLS", "1")

    class Provider:
        native_tools = True

        def __init__(self):
            self.index = 0

        def reply(self):
            self.index += 1
            return AssistantTurn(tool_calls=(ProviderToolCall(f"call-{self.index}", "read_file", {"path": "a.py"}),))

        def send_turn(self, *args):
            return self.reply()

        def send_tool_results(self, *args):
            return self.reply()

        def acknowledge_tool_results(self, *args, **kwargs):
            return AssistantTurn() if ending == "no_progress" else self.reply()

    request = KernelRunRequest(
        transport=KernelTransportDeps(
            provider=KernelRecordedProvider(Provider(), sink),
            provider_id="local",
            run_id="r",
            effect_scope="task",
            user_task="Inspect a file",
            stagnant_turns=1,
        ),
        execution=KernelExecutionDeps(
            project_path=project,
            managed_outputs=managed,
            session_id="s",
            workspace_revision_store=revs,
            durable=m.durable,
        ),
        observation=KernelObservationDeps(
            intent_sink=sink,
            on_boundary=durable_generation_boundary_callback(durable=m.durable, session_id="s", task_id="r", sink=sink),
        ),
    )
    try:
        result = run_task_kernel(session, request=request)
        assert result.stop_reason == ("no_progress" if ending == "no_progress" else "protocol"), result
        assert sink.pending_provider_settlement() is None
        assert all(effect.settlement is not None for effect in m.durable.effects("r"))
        m.mark_writer_settled("s", "r", provider_id="local", turns_used=result.turns, stop_reason=result.stop_reason)
    finally:
        m.durable.close()
