"""Production replay always re-enters execute_turn and consumes original receipts."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import NoReturn, Protocol

import pytest

from codey.codemode.kernel import build_execute_turn_executor
from codey.codemode.runtime import CodeModeRuntime, DeliveryRecorder, NodeWorkerRuntime, ToolInvoker
from codey.codemode.state import CodeModeStateStore
from codey.codemode.types import CodeModeResult, DeliveryReference, ToolCatalogEntry
from codey.durable import DurableSession, SQLiteDurableStore
from codey.operations import tool_operation
from codey.operations.kernel.kernel_execution import execute_turn
from codey.operations.kernel_contracts import ToolExecutor
from codey.operations.provider_session import adapt_provider
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.protocols.codemode import CodeModeLimits
from codey.providers.base import AssistantTurn
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.storage.managed_outputs import ManagedOutputStore
from codey.toolchain.tool_spec import ToolSpec
from codey.types.json_value import canonical_json_object, decode_json_object, parse_json_object
from codey.workspace.revision import WorkspaceRevisionStore


def _unexpected_vm(*args: object, **kwargs: object) -> NoReturn:
    pytest.fail("restarted settled VM")


def _child_refs(result: ToolResult) -> list[str]:
    from codey.types.json_value import require_json_strings

    return require_json_strings(result.canonical["child_effect_ids"], "child_effect_ids")


def _durable(line: RuntimeMutationLine) -> DurableSession:
    assert isinstance(line.durable, DurableSession)
    return line.durable


def _changed_digest(_runtime: NodeWorkerRuntime) -> str:
    return "changed-worker"


def _artifact_size(path: Path) -> int:
    return path.stat().st_size


def _new_session(project: Path) -> TaskSession:
    return TaskSession(TaskPolicy(grants=frozenset({"control", "project.read", "project.write", "project.verify"})),
                       task_kind="project", project=str(project), max_turns=8)


def fixture(tmp_path: Path, *, task: str = "recovery fixture", policy: TaskPolicy | None = None,
            ) -> tuple[Path, Path, Path, RuntimeMutationLine, ManagedOutputStore,
                                     WorkspaceRevisionStore, KernelEffectSink, TaskSession]:
    project, state, logdir = (tmp_path / name for name in ("project", "state", "log"))
    for directory in (project, state, logdir):
        directory.mkdir(parents=True)
    (project / "a.py").write_text("needle\n", encoding="utf-8")
    durable = DurableSession(SQLiteDurableStore(state / "durable.sqlite3"))
    durable.admit_input(conversation_id="s", request_id="request-r", task_id="r", text=task,
                        provider_snapshot={"provider_id": "local", "model_id": "fixture"},
                        task_state={"task_kind": "project", "project": str(project)})
    durable.start_task("r", state={"task_kind": "project"})
    mutations = RuntimeMutationLine(durable=durable)
    mutations.accept_operation(session_id="s", run_id="r", project=str(project), provider_id="local",
                               turn_budget=20, max_repair_rounds=1, task_kind="project")
    mutations.mark_writer_running("s", "r", provider_id="local")
    managed, revisions = ManagedOutputStore(state), WorkspaceRevisionStore(state)
    sink = KernelEffectSink(mutations, session_id="s", run_id="r", provider_id="local",
                            managed_outputs=managed, durable=durable)
    session = _new_session(project)
    if policy is not None:
        session.policy, session.task_text = policy, task
    from codey.operations.recovery import record_entry_policy

    record_entry_policy(mutations, session_id="s", run_id="r", policy=session.policy, durable=durable)
    return project, state, logdir, mutations, managed, revisions, sink, session


def builder(project: Path, mutations: RuntimeMutationLine, managed: ManagedOutputStore,
            revisions: WorkspaceRevisionStore, sink: KernelEffectSink, session: TaskSession, *,
            limits: CodeModeLimits | None = None,
            runtime_factory: Callable[[], CodeModeRuntime] | None = None) -> ToolExecutor:
    return build_execute_turn_executor(
        session=session, snapshot=build_turn_snapshot(session), run_id="r", effect_scope="outer", turn=0,
        project_path=project, managed_outputs=managed, workspace_revision_store=revisions,
        intent_sink=sink, durable=_durable(mutations), session_id="s", limits=limits,
        runtime_factory=runtime_factory,
    )


def test_completed_parent_restores_original_receipt_without_vm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, state, logdir, m, managed, revs, sink, session = fixture(tmp_path)
    call = ToolCall("codemode", {"code": "return await tools.read_file({path:'a.py'});"}, call_id="parent")
    parent = builder(project, m, managed, revs, sink, session)
    def execute_parent() -> ToolResult:
        return execute_turn(session, [call],
        executors={"codemode": parent},
        run_id="r",
        effect_scope="outer",
        turn=0,
        snapshot=build_turn_snapshot(session),
        intent_sink=sink,
        project_path=project,
        managed_outputs=managed,
        workspace_revision_store=revs,
        durable=_durable(m),
        )[0]
    try:
        first = execute_parent()
        assert first.ok, first.model_text
        monkeypatch.setattr(NodeWorkerRuntime, "execute", _unexpected_vm)
        second = execute_parent()
        assert second.ok, second.model_text
        assert second.model_text == first.model_text
        assert second.canonical == first.canonical
        assert len(_child_refs(second)) == 1
        checkpoint_state = _durable(m).task("r").state
        assert "__execution__" not in json.dumps(dict[str, object](checkpoint_state), default=str)
        assert "needle" not in json.dumps(canonical_json_object(checkpoint_state.get("codemode_replay", {})), default=str)
    finally:
        _durable(m).close()


@pytest.mark.parametrize("continuation", [
    "return 1;",
    "try { await store('after_failure', true); } catch {} return await tools.read_file({path:'a.py'});",
    "try { await tools.read_file({path:'a.py'}); } catch {} return await store('after_failure', true);",
])
def test_missing_child_receipt_blocks_instead_of_reexecuting(tmp_path: Path, continuation: str) -> None:
    from codey.operations.task_effects import KernelEffectSink
    from codey.operations.task_session import turn_effect_id
    from codey.runtime.core.codemode_identity import parent_execution_id

    project, state, logdir, m, managed, revs, sink, session = fixture(tmp_path)
    call = ToolCall(
        "codemode", {"code": "try { await tools.read_file({path:'a.py'}); } catch {} " + continuation}, call_id="parent"
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
        m, session_id="s", run_id="r", provider_id="local", managed_outputs=managed, durable=_durable(m)
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
            durable=_durable(m),
        )[0]
        assert not result.ok
        assert "receipt is missing or uncertain" in result.model_text
        assert _durable(m).effect(child_id).settlement is None
        assert len(_durable(m).effects("r")) == 2
        assert "after_failure" not in decode_json_object(_durable(m).task("r").state.get("codemode_state", {}))
        assert "children" not in str(_durable(m).task("r").state.get("codemode_replay", {}))
    finally:
        _durable(m).close()


def test_normal_child_failure_allows_state_and_later_read(tmp_path: Path) -> None:
    project, _, _, m, managed, revs, sink, session = fixture(tmp_path)
    call = ToolCall("codemode", {"code": """
        const missing = await tools.read_file({path:'missing.py'});
        await store('handled', !missing.ok);
        return await tools.read_file({path:'a.py'});
    """}, call_id="parent")
    try:
        result = execute_turn(session, [call], executors={"codemode": builder(project, m, managed, revs, sink, session)},
            run_id="r", effect_scope="outer", turn=0, snapshot=build_turn_snapshot(session),
            intent_sink=sink, durable=_durable(m))[0]
        assert result.ok, result.model_text
        assert decode_json_object(_durable(m).task("r").state["codemode_state"])["handled"] is True
        assert len(_durable(m).effects("r")) == 3
        assert all(effect.settlement is not None for effect in _durable(m).effects("r"))
    finally:
        _durable(m).close()


@pytest.mark.parametrize("already_admitted", [False, True])
def test_parallel_invalidation_is_atomic_with_intent_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, already_admitted: bool,
) -> None:
    from threading import Event

    from codey.codemode.replay import CodeModeReplayError, DurableCodeModeReplay
    from codey.operations.task_effects import KernelEffectSink

    project, _, _, m, managed, revs, sink, session = fixture(tmp_path)
    reached, invalidated = Event(), Event()
    original_child, original_begin = DurableCodeModeReplay.child, KernelEffectSink.begin_turn

    def child(replay: DurableCodeModeReplay, call: ToolCall, effect_id: str) -> None:
        original_child(replay, call, effect_id)
        if call.call_id.endswith(":child:1"):
            assert reached.wait(5)
            replay.invalidate("first fatal recovery cause")
            replay.invalidate("later error must not replace it")
            invalidated.set()
            raise CodeModeReplayError("later error must not replace it")
        if not already_admitted:
            reached.set()
            assert invalidated.wait(5)

    def begin(active_sink: KernelEffectSink, items: list[tuple[str, ToolCall, int]], *, turn: int,
              specs: Mapping[str, ToolSpec] | None = None, scope: str = "") -> None:
        original_begin(active_sink, items, turn=turn, specs=specs, scope=scope)
        if active_sink.parent_effect_id and already_admitted:
            reached.set()
            assert invalidated.wait(5)

    monkeypatch.setattr(DurableCodeModeReplay, "child", child)
    monkeypatch.setattr(KernelEffectSink, "begin_turn", begin)
    call = ToolCall("codemode", {"code": """
        await Promise.allSettled([
          tools.read_file({path:'a.py'}), tools.read_file({path:'a.py'})
        ]);
        try { await store('after_failure', true); } catch {}
        return 1;
    """}, call_id="parent")
    try:
        result = execute_turn(session, [call], executors={"codemode": builder(project, m, managed, revs, sink, session)},
            run_id="r", effect_scope="outer", turn=0, snapshot=build_turn_snapshot(session),
            intent_sink=sink, durable=_durable(m))[0]
        assert not result.ok
        assert "first fatal recovery cause" in result.model_text
        assert "later error must not replace" not in result.model_text
        effects = _durable(m).effects("r")
        assert len(effects) == (2 if already_admitted else 1)
        assert all(effect.settlement is not None for effect in effects)
        assert "after_failure" not in decode_json_object(_durable(m).task("r").state.get("codemode_state", {}))
    finally:
        _durable(m).close()


def test_parallel_result_delivery_and_child_identity_are_deterministic() -> None:
    from codey.codemode.host import CodeModeHost
    from codey.codemode.types import ToolCatalogEntry
    from codey.runtime.core.models import ToolResult

    observed: list[tuple[str, str]] = []
    catalog = (ToolCatalogEntry("read_file", "read", parallel_safe=True),)

    def execute(call: ToolCall) -> ToolResult:
        # Completion order deliberately differs from issuance order.
        time.sleep(0.05 if call.args["path"] == "slow" else 0)
        path = call.args["path"]
        assert isinstance(path, str)
        observed.append((call.call_id, path))
        return ToolResult(ok=True, call=call, model_text=path)

    host = CodeModeHost(execution_id="cm", catalog=catalog, execute=execute)
    result = NodeWorkerRuntime().execute(
        "return await Promise.race(['slow','fast'].map(path=>tools.read_file({path})));",
        catalog=catalog,
        invoke=host.invoke,
    )
    assert result.ok
    assert result.value == {"ok": True, "value": "fast"}
    assert set(observed) == {("cm:child:0", "slow"), ("cm:child:1", "fast")}


@pytest.mark.parametrize(("read_path", "tamper"), [
    (path, modification)
    for path in ("a.py", "./a.py") for modification in (False, True, "runtime", "engine")
] + [("package/file.py", False)])
def test_formal_resume_reconstructs_pending_parent_without_leaf_reexecution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_path: str, tamper: bool | str,
) -> None:
    from codey.agents.tools import AgentToolFns
    from codey.codemode.runtime import NodeWorkerRuntime
    from codey.operations.generation_boundary import durable_generation_boundary_callback
    from codey.operations.kernel_contracts import (
        KernelExecutionDeps,
        KernelObservationDeps,
        KernelRunRequest,
        KernelTransportDeps,
    )
    from codey.operations.task_effects import KernelEffectSink
    from codey.operations.task_loop import run_task_kernel
    from codey.providers.base import AssistantTurn, ProviderToolCall
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    class Crash(BaseException):
        pass

    project, state, logdir, m, managed, revs, sink, session = fixture(tmp_path)
    scoped = read_path == "package/file.py"
    if scoped:
        (project / "package").mkdir()
        (project / "package/AGENTS.md").write_text("Use SCOPED = 29 for this package.\n", encoding="utf-8")
        (project / read_path).write_bytes(b"SCOPED = 13\n")
    call = ToolCall("codemode", {"code": """
        const n = (await load('count')) ?? 0;
        await store('count', n+1);
        const r = await tools.read_file({path:inputs.path});
        return {n,r,text:inputs.text,now:Date.now(),rand:Math.random()};
    """, "inputs": {"path": read_path, "text": "quoted data"}}, call_id="parent")
    if tamper is True:
        (project / "a.py").write_text("needle" * 3000, encoding="utf-8")
    original_values: list[object] = []
    original = NodeWorkerRuntime.execute

    def interrupted(self: NodeWorkerRuntime, code: str, *, catalog: Sequence[ToolCatalogEntry],
                    invoke: ToolInvoker, inputs: Mapping[str, object] | None = None,
                    state_store: CodeModeStateStore | None = None, replay_context: Mapping[str, object] | None = None,
                    delivery: Sequence[DeliveryReference] = (), on_delivery: DeliveryRecorder | None = None,
                    limits: CodeModeLimits | None = None) -> CodeModeResult:
        result = original(self, code, catalog=catalog, invoke=invoke, inputs=inputs, state_store=state_store,
                          replay_context=replay_context, delivery=delivery, on_delivery=on_delivery, limits=limits)
        assert result.ok
        original_values.append(result.value)
        raise Crash()

    class Provider:
        native_tools = True
        edit_requested = False

        def send_turn(self, *args: object) -> AssistantTurn:
            return AssistantTurn(tool_calls=(ProviderToolCall(call.call_id, call.name, call.args),))

        def send_tool_results(self, *args: object, **kwargs: object) -> AssistantTurn:
            if scoped and not self.edit_requested:
                assert "Use SCOPED = 29 for this package." in repr((args, kwargs))
                self.edit_requested = True
                return AssistantTurn(tool_calls=(ProviderToolCall("edit", "edit", {
                    "path": read_path, "replacements": [{"old_string": "SCOPED = 13", "new_string": "SCOPED = 29"}],
                }),))
            return AssistantTurn(tool_calls=(ProviderToolCall("done", "done", {"summary": "finished"}),))

        def acknowledge_tool_results(self, *args: object) -> AssistantTurn:
            return AssistantTurn()

    def request(active_sink: KernelEffectSink) -> KernelRunRequest:
        return KernelRunRequest(transport=KernelTransportDeps(provider=adapt_provider(Provider()), provider_id="local", run_id="r", effect_scope="outer"),
            execution=KernelExecutionDeps(
                project_path=project,
                tool_fns=AgentToolFns(),
                workspace_revision_store=revs,
            ),
            observation=KernelObservationDeps(intent_sink=active_sink, on_boundary=durable_generation_boundary_callback(
                durable=active_sink.durable, session_id="s", task_id="r", sink=active_sink)))

    monkeypatch.setattr(NodeWorkerRuntime, "execute", interrupted)
    with pytest.raises(Crash):
        run_task_kernel(session, request=request(sink))
    _durable(m).close()
    if tamper is True:
        artifact = max((p for p in managed.root.rglob("*") if p.is_file()), key=_artifact_size)
        artifact.write_text("corrupt original receipt", encoding="utf-8")
    monkeypatch.setattr(NodeWorkerRuntime, "execute", original)
    if tamper == "runtime":
        monkeypatch.setattr(NodeWorkerRuntime, "contract_digest", _changed_digest)
    if tamper == "engine":
        import subprocess

        class Probe(Protocol):
            def __call__(self, args: list[str], *, check: bool, capture_output: bool, text: bool,
                         encoding: str, timeout: float) -> subprocess.CompletedProcess[str]: ...

        original_probe: Probe = subprocess.run

        def changed_engine(args: list[str], *, check: bool, capture_output: bool, text: bool,
                           encoding: str, timeout: float) -> subprocess.CompletedProcess[str]:
            result = original_probe(args, check=check, capture_output=capture_output, text=text,
                                    encoding=encoding, timeout=timeout)
            identity = parse_json_object(result.stdout)
            identity["node"] = "different-node-version"
            result.stdout = json.dumps(identity)
            return result

        monkeypatch.setattr(subprocess, "run", changed_engine)
        monkeypatch.setenv("CODEY_TEST_CHANGED_NODE_ENVIRONMENT", "1")
    durable = DurableSession(SQLiteDurableStore(state / "durable.sqlite3"))
    durable.recover_scheduler_state()
    if durable.task("r").status == "pending":
        durable.claim_task("r")
    line = RuntimeMutationLine(durable=durable)
    resumed_sink = KernelEffectSink(line, session_id="s", run_id="r", provider_id="local", durable=durable, managed_outputs=managed)
    fresh = _new_session(project)
    fresh.verification_forbidden = scoped
    invoke = tool_operation._run_via_delegate_or_fn

    from codey.operations.tool_operation import ToolDelegate

    def no_dispatch(delegate: ToolDelegate | None, runnable: Mapping[str, ToolExecutor], session: TaskSession,
                    call: ToolCall, name: str, *, active_turn: int, tool_index: int,
                    project_path: Path | None = None) -> tuple[ToolResult, bool, int | None]:
        if name == "read_file":
            pytest.fail("re-executed committed child")
        return invoke(delegate, runnable, session, call, name, active_turn=active_turn,
                      tool_index=tool_index, project_path=project_path)

    monkeypatch.setattr(tool_operation, "_run_via_delegate_or_fn", no_dispatch)
    try:
        result = run_task_kernel(fresh, request=request(resumed_sink))
        parents = [effect for effect in durable.effects("r") if effect.intent.get("tool_name") == "codemode"]
        if tamper:
            assert not result.completed or parents[0].status == "error"
            assert decode_json_object(durable.task("r").state["codemode_state"])["count"] == 1
            return
        assert result.completed, (result, [(row.call.name, row.model_text) for row in fresh._memory_results.values()])
        restored = fresh._memory_results[parents[0].effect_id]
        assert restored.canonical["value"] == original_values[0]
        assert len(_child_refs(restored)) == 1
        if scoped:
            assert (project / read_path).read_bytes() == b"SCOPED = 29\n", [
                (row.call.name, row.model_text) for row in fresh._memory_results.values()
            ]
        assert decode_json_object(durable.task("r").state["codemode_state"])["count"] == 1
        assert all(effect.settlement is not None for effect in durable.effects("r"))
        # Running the same settled invocation again cannot re-enter the VM.
        monkeypatch.setattr(NodeWorkerRuntime, "execute", _unexpected_vm)
        assert run_task_kernel(_new_session(project), request=request(resumed_sink)) == result
    finally:
        durable.close()


@pytest.mark.parametrize("ending", ["no_progress", "budget_drain"])
def test_terminal_native_receipt_drain_settles_received_generation_before_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str,
) -> None:
    from codey.operations.generation_boundary import durable_generation_boundary_callback
    from codey.operations.kernel_contracts import (
        KernelExecutionDeps,
        KernelObservationDeps,
        KernelRunRequest,
        KernelTransportDeps,
    )
    from codey.operations.task_loop import run_task_kernel
    from codey.providers.base import AssistantTurn, ProviderToolCall

    project, state, logdir, m, managed, revs, sink, session = fixture(tmp_path)
    session.max_turns = 4 if ending == "no_progress" else 1
    monkeypatch.setenv("NATIVE_TOOLS", "1")

    class Provider:
        native_tools = True

        def __init__(self) -> None:
            self.index = 0

        def reply(self) -> AssistantTurn:
            self.index += 1
            return AssistantTurn(tool_calls=(ProviderToolCall(f"call-{self.index}", "read_file", {"path": "a.py"}),))

        def send_turn(self, *args: object) -> AssistantTurn:
            return self.reply()

        def send_tool_results(self, *args: object) -> AssistantTurn:
            return self.reply()

        def acknowledge_tool_results(self, *args: object, **kwargs: object) -> AssistantTurn:
            return AssistantTurn() if ending == "no_progress" else self.reply()

    request = KernelRunRequest(
        transport=KernelTransportDeps(
            provider=adapt_provider(Provider()),
            provider_id="local",
            run_id="r",
            effect_scope="task",
            user_task="Inspect a file",
            stagnant_turns=1,
        ),
        execution=KernelExecutionDeps(
            project_path=project,
            workspace_revision_store=revs,
        ),
        observation=KernelObservationDeps(
            intent_sink=sink,
            on_boundary=durable_generation_boundary_callback(durable=_durable(m), session_id="s", task_id="r", sink=sink),
        ),
    )
    try:
        result = run_task_kernel(session, request=request)
        assert result.stop_reason == ("no_progress" if ending == "no_progress" else "protocol"), result
        assert sink.pending_provider_settlement() is None
        assert all(effect.settlement is not None for effect in _durable(m).effects("r"))
        m.mark_writer_settled("s", "r", provider_id="local", turns_used=result.turns, stop_reason=result.stop_reason)
    finally:
        _durable(m).close()
