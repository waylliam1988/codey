"""Cross-layer delivery, repair and cancellation over the production worker."""
from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

from codey.codemode.host import CodeModeHost
from codey.codemode.kernel import build_execute_turn_executor
from codey.codemode.replay import DurableCodeModeReplay
from codey.codemode.runtime import CodeModeRequest, CodeModeSession, NodeWorkerRuntime
from codey.codemode.state import DurableCodeModeState
from codey.codemode.types import DeliveryReference, ToolCatalogEntry
from codey.durable import DurableSession, SQLiteDurableStore
from codey.operations.browser.turn_surface import offer_browser_tools
from codey.operations.generation_boundary import durable_generation_boundary_callback
from codey.operations.kernel.kernel_execution import execute_turn
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
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.protocols.codemode import CodeModeLimits
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.types.json_value import JsonObject, canonical_json_object, decode_json_object, require_json_string
from tests.test_codemode_replay_authority import _durable, _new_session, fixture


def _run(session: TaskSession, project: Path, call: ToolCall, turn: int,
         executors: Mapping[str, ToolExecutor], *, limits: CodeModeLimits | None = None) -> ToolResult:
    snapshot = offer_browser_tools(build_turn_snapshot(session), session)
    parent = build_execute_turn_executor(session=session, snapshot=snapshot, run_id="closure", effect_scope="task",
        turn=turn, project_path=project, executors=executors, limits=limits)
    return execute_turn(session, [call], executors={"codemode": parent}, run_id="closure", effect_scope="task",
                        turn=turn, project_path=project, snapshot=snapshot)[0]


def _read(call: ToolCall) -> ToolResult:
    path = require_json_string(call.args["path"], "path")
    return ToolResult(call, path, ok=True, canonical={"value": {"path": path, "text": path,
        "start_line": 1, "end_line": 1, "total_lines": 1, "next_offset": 0, "text_complete": True}})


def _changed_runtime(_runtime: NodeWorkerRuntime) -> str:
    return "changed-runtime"


@pytest.mark.parametrize("ambient", [False, True])
def test_return_repair_inherits_delivery_and_ambient_context(tmp_path: Path, ambient: bool) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read"})), project=str(tmp_path))
    calls: list[ToolCall] = []

    def read(call: ToolCall) -> ToolResult:
        calls.append(call)
        if str(call.args["path"]).startswith("A"):
            time.sleep(0.15)
        return _read(call)

    suffix = "+ '_' + Date.now() + '_' + Math.random()" if ambient else ""
    prefix = (f"const jobs=[tools.read_file({{path:'A'{suffix}}}),tools.read_file({{path:'B'{suffix}}})];"
              "const winner=await Promise.race(jobs); await Promise.all(jobs);")
    failed = _run(session, tmp_path, ToolCall("codemode", {"code": prefix + "return winner.value.missing;"}, "first"),
                  0, {"read_file": read})
    assert not failed.ok and failed.audit["error_code"] == "missing_return", failed.model_text
    token = decode_json_object(failed.canonical["repair"])["token"]
    repaired = _run(session, tmp_path, ToolCall("codemode", {
        "code": prefix + "return winner.value.text;", "repair_of": token}, "repair"), 1, {"read_file": read})
    assert repaired.ok, repaired.model_text
    assert str(repaired.canonical["value"]).startswith("B")
    assert repaired.audit["recovery_disposition"] == "receipt_replay" and len(calls) == 2


def test_repair_rejects_changed_execution_limits(tmp_path: Path) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read"})), project=str(tmp_path))
    calls: list[ToolCall] = []

    def read(call: ToolCall) -> ToolResult:
        calls.append(call)
        return _read(call)

    prefix = "const read=await tools.read_file({path:'A'});"
    failed = _run(session, tmp_path, ToolCall("codemode", {"code": prefix + "return read.value.missing;"}, "first"),
                  0, {"read_file": read})
    token = decode_json_object(failed.canonical["repair"])["token"]
    repaired = _run(session, tmp_path, ToolCall("codemode", {
        "code": prefix + "return read.value.text;", "repair_of": token}, "repair"),
        1, {"read_file": read}, limits=CodeModeLimits(max_child_calls=2, max_concurrent_calls=2))
    assert not repaired.ok and repaired.audit["error_code"] == "repair_rejected"
    assert "runtime contract changed" in repaired.model_text and len(calls) == 1


@pytest.mark.parametrize("changed_runtime", [False, True])
def test_durable_repair_uses_original_journal_context_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_runtime: bool,
) -> None:
    project, _, _, line, _, revisions, sink, session = fixture(tmp_path)
    durable = _durable(line)
    state = DurableCodeModeState(durable, "r")
    state.store("version", "original")
    calls: list[ToolCall] = []
    recorded: list[JsonObject] = []
    prefix = ("const version=await load('version'); const suffix='_'+version+'_'+Date.now()+'_'+Math.random();"
              "const jobs=[tools.read_file({path:'A'+suffix}),tools.read_file({path:'B'+suffix})];"
              "const winner=await Promise.race(jobs); await Promise.all(jobs);")

    def read(call: ToolCall) -> ToolResult:
        calls.append(call)
        if str(call.args["path"]).startswith("A"):
            time.sleep(0.15)
        return _read(call)

    class Provider:
        native_tools = True

        def send_turn(self, *args: object) -> AssistantTurn:
            return AssistantTurn(tool_calls=(ProviderToolCall("first", "codemode", {
                "code": prefix + "return winner.value.missing;"}),))

        def send_tool_results(self, *args: object) -> AssistantTurn:
            if not recorded:
                failed, = [result for result in session._memory_results.values() if result.call.name == "codemode"]
                assert not failed.ok and failed.audit["error_code"] == "missing_return", failed.model_text
                recorded.append(canonical_json_object(durable.task("r").state["codemode_replay"]))
                state.store("version", "changed")
                if changed_runtime:
                    monkeypatch.setattr(NodeWorkerRuntime, "contract_digest", _changed_runtime)
                return AssistantTurn(tool_calls=(ProviderToolCall("repair", "codemode", {
                    "code": prefix + "return winner.value.text;",
                    "repair_of": decode_json_object(failed.canonical["repair"])["token"]}),))
            return AssistantTurn(tool_calls=(ProviderToolCall("done", "done", {"summary": "finished"}),))

        def acknowledge_tool_results(self, *args: object) -> AssistantTurn:
            return AssistantTurn()

    request = KernelRunRequest(
        transport=KernelTransportDeps(provider=adapt_provider(Provider()), provider_id="local",
                                      run_id="r", effect_scope="outer"),
        execution=KernelExecutionDeps(intent_sink=sink, project_path=project, workspace_revision_store=revisions,
                                      executors={"read_file": read}),
        observation=KernelObservationDeps(on_boundary=durable_generation_boundary_callback(
            durable=durable, session_id="s", task_id="r", sink=sink)),
    )
    try:
        run_task_kernel(session, request=request)
        repaired, = [result for result in session._memory_results.values() if result.call.call_id == "repair"]
        if changed_runtime:
            assert not repaired.ok and repaired.audit["error_code"] == "repair_rejected", repaired.model_text
            assert "runtime contract" in repaired.model_text
        else:
            assert repaired.ok, repaired.model_text
            assert str(repaired.canonical["value"]).startswith("B_original_")
            assert repaired.audit["recovery_disposition"] == "receipt_replay"
        assert len(calls) == 2
        assert len([effect for effect in durable.effects("r") if effect.intent.get("parent_effect_id")]) == 2
        assert canonical_json_object(durable.task("r").state["codemode_replay"]) == recorded[0]
        assert len(recorded[0]) == 1 and state.load("version") == "changed"
    finally:
        durable.close()


def test_return_repair_preserves_reserved_child_index_gaps(tmp_path: Path) -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "web.read", "browser.observe", "browser.interact"})),
                          task_kind="hybrid", project=str(tmp_path))
    calls: list[ToolCall] = []

    def click(call: ToolCall) -> ToolResult:
        calls.append(call)
        return ToolResult(call, "click not delivered", ok=False)

    def read(call: ToolCall) -> ToolResult:
        calls.append(call)
        return _read(call)

    prefix = "await tools.browser_click({ref:'observed',purpose:'result'}); const read=await tools.read_file({path:'A'});"
    failed = _run(session, tmp_path, ToolCall("codemode", {"code": prefix + "return read.value.missing;"}, "first"),
                  0, {"browser_click": click, "read_file": read})
    assert not failed.ok and failed.audit["error_code"] == "missing_return", failed.model_text
    assert [call.call_id.rsplit(":", 1)[-1] for call in calls] == ["0", "2"]
    token = decode_json_object(failed.canonical["repair"])["token"]
    repaired = _run(session, tmp_path, ToolCall("codemode", {
        "code": prefix + "return read.value.text;", "repair_of": token}, "repair"),
        1, {"browser_click": click, "read_file": read})
    assert repaired.ok and repaired.canonical["value"] == "A", repaired.model_text
    assert len(calls) == 2


def test_terminate_closes_each_actual_followup_and_settles_started_click() -> None:
    admitted, release, settled = threading.Event(), threading.Event(), threading.Event()
    calls: list[ToolCall] = []
    catalog = (ToolCatalogEntry("browser_click", "click", parameters={"x-codey-click-plans": {
        "result": [{"name": "browser_read", "args": {}}]}}, namespace="browser",
        read_only=False, grant="browser.interact"), ToolCatalogEntry("browser_read", "read"))

    def execute(call: ToolCall) -> ToolResult:
        calls.append(call)
        if call.name == "browser_click":
            admitted.set()
            assert release.wait(3)
            settled.set()
        return ToolResult(call, "settled", ok=True)

    host = CodeModeHost(execution_id="cancel-composite", catalog=catalog, execute=execute, allow_writes=True)
    session = CodeModeSession()
    handle = session.start(CodeModeRequest("return await tools.browser_click({ref:'r',purpose:'result'});",
                                           catalog, host.invoke))
    try:
        assert admitted.wait(3)
        assert session.terminate(handle, reason="stop composite").result is None
    finally:
        release.set()
    result = session.outcome(handle).result
    for _ in range(50):
        if result is not None:
            break
        session.wait(handle, yield_ms=100)
        result = session.outcome(handle).result
    assert result is not None and result.error_code == "terminated"
    assert settled.is_set() and [call.name for call in calls] == ["browser_click"]


def test_terminal_rejects_unissued_delivery_without_waiting_for_deadline() -> None:
    def invoke(_name: str, args: dict[str, object]) -> object:
        if args["path"] == "slow":
            time.sleep(0.1)
        return args["path"]

    delivery: tuple[DeliveryReference, ...] = ({"call_id": "call-2", "child_index": 1},
                                               {"call_id": "call-3", "child_index": 2})
    result = NodeWorkerRuntime().execute(
        "return await Promise.race([tools.read({path:'slow'}),tools.read({path:'fast'})]);",
        catalog=(ToolCatalogEntry("read", "read"),), invoke=invoke, limits=CodeModeLimits(timeout_ms=1500),
        delivery=delivery,
    )
    assert not result.ok and result.error_code == "delivery_journal", result
    assert "before issuing" in result.error


def test_sqlite_resume_drains_journal_after_pure_race_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Crash(BaseException):
        pass

    project, state, _, line, managed, revisions, sink, session = fixture(tmp_path)
    (project / "b.py").write_text("B\n", encoding="utf-8")
    call = ToolCall("codemode", {"code":
        "return await Promise.race([tools.read_file({path:'a.py'}),tools.read_file({path:'b.py'})]);"}, "race")
    original_invoke, original_record = CodeModeHost.invoke, DurableCodeModeReplay.record_delivery
    settled = threading.Event()
    recovering = False

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
            execution=KernelExecutionDeps(intent_sink=active_sink, project_path=project, workspace_revision_store=revisions),
            observation=KernelObservationDeps(on_boundary=durable_generation_boundary_callback(
                durable=active_sink.durable, session_id="s", task_id="r", sink=active_sink)),
        )

    def delayed(host: CodeModeHost, name: str, args: dict[str, object]) -> object:
        if args.get("path") == "a.py":
            time.sleep(0.2)
        value = original_invoke(host, name, args)
        if args.get("path") == "a.py":
            settled.set()
        return value

    def crash_after_frontier(replay: DurableCodeModeReplay, call_id: str, child_index: int | None) -> None:
        original_record(replay, call_id, child_index)
        if not recovering and call_id == "call-2":
            assert settled.wait(3)
            original_record(replay, "call-1", 0)
            raise Crash()

    monkeypatch.setattr(CodeModeHost, "invoke", delayed)
    monkeypatch.setattr(DurableCodeModeReplay, "record_delivery", crash_after_frontier)
    try:
        with pytest.raises(Crash):
            run_task_kernel(session, request=request(sink))
        records = canonical_json_object(_durable(line).task("r").state["codemode_replay"])
        record = decode_json_object(next(iter(records.values())))
        assert record["delivery"] == [{"call_id": "call-2", "child_index": 1}, {"call_id": "call-1", "child_index": 0}]
    finally:
        _durable(line).close()
    recovering = True
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
        parent, = [effect for effect in durable.effects("r") if effect.intent.get("tool_name") == "codemode"]
        result = fresh._memory_results[parent.effect_id]
        assert result.ok, result.model_text
        assert outcome.completed, outcome
        assert decode_json_object(decode_json_object(result.canonical["value"])["value"])["text"] == "B\n"
        assert len([effect for effect in durable.effects("r") if effect.intent.get("parent_effect_id")]) == 2
    finally:
        durable.close()
