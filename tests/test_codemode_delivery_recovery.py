"""Delivery recovery reuses kernel receipts through both crash windows."""
from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

from codey.codemode.host import CodeModeHost
from codey.codemode.replay import DurableCodeModeReplay
from codey.durable import DurableSession, SQLiteDurableStore
from codey.operations.generation_boundary import durable_generation_boundary_callback
from codey.operations.kernel import kernel_execution
from codey.operations.kernel.kernel_execution import ToolDelegate
from codey.operations.kernel_contracts import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelRunRequest,
    KernelTransportDeps,
    ToolExecutor,
)
from codey.operations.provider_session import adapt_provider
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_loop import run_task_kernel
from codey.operations.task_session import TaskSession
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.types.json_value import canonical_json_object, decode_json_object
from tests.test_codemode_replay_authority import _durable, _new_session, fixture


@pytest.mark.parametrize("journaled", [False, True])
def test_restart_reuses_settled_effects_and_original_delivery_frontier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, journaled: bool,
) -> None:
    class Crash(BaseException):
        pass

    project, state, _, line, managed, revisions, sink, session = fixture(tmp_path)
    (project / "b.py").write_text("fast receipt\n", encoding="utf-8")
    call = ToolCall("codemode", {"code": """
        await store('seen', true);
        const jobs = [tools.read_file({path:'a.py'}), tools.read_file({path:'b.py'})];
        const winner = await Promise.race(jobs);
        await Promise.all(jobs);
        return {winner, seen:await load('seen')};
    """}, call_id="delivery-parent")

    class Provider:
        native_tools = True

        def send_turn(self, *args: object) -> AssistantTurn:
            return AssistantTurn(tool_calls=(ProviderToolCall(call.call_id, call.name, call.args),))

        def send_tool_results(self, *args: object) -> AssistantTurn:
            return AssistantTurn(tool_calls=(ProviderToolCall("done", "done", {"summary": "finished"}),))

        def acknowledge_tool_results(self, *args: object) -> AssistantTurn:
            return AssistantTurn()

    def request(active_sink: KernelEffectSink) -> KernelRunRequest:
        return KernelRunRequest(
            transport=KernelTransportDeps(provider=adapt_provider(Provider()), provider_id="local",
                                          run_id="r", effect_scope="outer"),
            execution=KernelExecutionDeps(project_path=project, workspace_revision_store=revisions),
            observation=KernelObservationDeps(intent_sink=active_sink, on_boundary=durable_generation_boundary_callback(
                durable=active_sink.durable, session_id="s", task_id="r", sink=active_sink)),
        )

    original_invoke, original_record = CodeModeHost.invoke, DurableCodeModeReplay.record_delivery
    recovering = False
    slow_settled = threading.Event()

    def delayed(host: CodeModeHost, name: str, args: dict[str, object]) -> object:
        slow = "b.py" if recovering else "a.py"
        if name == "read_file" and args.get("path") == slow:
            time.sleep(0.2)
        result = original_invoke(host, name, args)
        if not recovering and name == "read_file" and args.get("path") == "a.py":
            slow_settled.set()
        return result

    def interrupted(replay: DurableCodeModeReplay, call_id: str, child_index: int | None) -> None:
        if call_id == "call-2":
            assert slow_settled.wait(3)
            if journaled:
                original_record(replay, call_id, child_index)
            raise Crash()
        original_record(replay, call_id, child_index)

    monkeypatch.setattr(CodeModeHost, "invoke", delayed)
    monkeypatch.setattr(DurableCodeModeReplay, "record_delivery", interrupted)
    try:
        with pytest.raises(Crash):
            run_task_kernel(session, request=request(sink))
        effects = _durable(line).effects("r")
        children = [effect for effect in effects if effect.intent.get("parent_effect_id")]
        assert len(children) == 2 and all(effect.settlement is not None for effect in children)
        replay_records = canonical_json_object(_durable(line).task("r").state["codemode_replay"])
        record = decode_json_object(next(iter(replay_records.values())))
        delivery = record["delivery"]
        assert isinstance(delivery, list)
        assert [decode_json_object(item)["call_id"] for item in delivery] == (
            ["state-1", "call-2"] if journaled else ["state-1"])
    finally:
        _durable(line).close()

    recovering = True
    monkeypatch.setattr(DurableCodeModeReplay, "record_delivery", original_record)
    invoke_leaf = kernel_execution._run_via_delegate_or_fn

    def no_read_dispatch(delegate: ToolDelegate | None, runnable: Mapping[str, ToolExecutor], session: TaskSession,
                         call: ToolCall, name: str, *, active_turn: int, tool_index: int,
                         project_path: Path | None = None) -> tuple[ToolResult, bool, int | None]:
        assert name != "read_file", "re-executed a settled child"
        return invoke_leaf(delegate, runnable, session, call, name, active_turn=active_turn,
                           tool_index=tool_index, project_path=project_path)

    monkeypatch.setattr(kernel_execution, "_run_via_delegate_or_fn", no_read_dispatch)
    durable = DurableSession(SQLiteDurableStore(state / "durable.sqlite3"))
    durable.recover_scheduler_state()
    if durable.task("r").status == "pending":
        durable.claim_task("r")
    resumed_line = RuntimeMutationLine(durable=durable)
    resumed_sink = KernelEffectSink(resumed_line, session_id="s", run_id="r", provider_id="local",
                                    managed_outputs=managed, durable=durable)
    fresh = _new_session(project)
    try:
        outcome = run_task_kernel(fresh, request=request(resumed_sink))
        assert outcome.completed, outcome
        parent, = [effect for effect in durable.effects("r") if effect.intent.get("tool_name") == "codemode"]
        result = fresh._memory_results[parent.effect_id]
        assert result.ok, result.model_text
        value = decode_json_object(result.canonical["value"])
        winner = decode_json_object(value["winner"])
        text = decode_json_object(winner["value"])["text"]
        # Without a journaled delivery the settled receipts may first deliver
        # in the new observation order. A committed frontier must win exactly.
        assert text == ("fast receipt\n" if journaled else "needle\n")
        assert value["seen"] is True
        assert len([effect for effect in durable.effects("r") if effect.intent.get("parent_effect_id")]) == 2
        assert all(effect.settlement is not None for effect in durable.effects("r"))
    finally:
        durable.close()
