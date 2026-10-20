from __future__ import annotations


def test_codemode_child_scopes_produce_distinct_durable_batches() -> None:
    from codey.operations.task_effects import KernelEffectSink
    from codey.runtime.core.models import ToolCall

    class Mutations:
        def __init__(self) -> None:
            self.batch_ids: list[str] = []

        def begin_tool_batch(self, _session_id: str, _run_id: str, *, delivery_intent: object, **_kwargs: object) -> None:
            self.batch_ids.append(str(delivery_intent.batch_id))

    mutations = Mutations()
    durable = type("Durable", (), {"effects": lambda _self, _run_id: ()})()
    sink = KernelEffectSink(mutations, session_id="s", run_id="r", provider_id="p", durable=durable)
    sink.begin_turn([("r:child:0", ToolCall("read_file", {"path": "a.py"}), 0)], turn=0, scope="cm:child:0")
    sink.begin_turn([("r:child:1", ToolCall("read_file", {"path": "b.py"}), 1)], turn=0, scope="cm:child:1")

    assert len(mutations.batch_ids) == 2
    assert mutations.batch_ids[0] != mutations.batch_ids[1]


def test_nested_execute_turn_passes_unique_child_scopes_to_intent_sink() -> None:
    from codey.codemode.kernel import build_execute_turn_executor
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall, ToolResult

    class Sink:
        def __init__(self) -> None:
            self.scopes: list[str] = []

        def begin_turn(self, _items: object, *, turn: int, specs: object = None, scope: str = "") -> None:
            self.scopes.append(scope)

        def settle(self, _identity: str, _ok: bool, *, result: object, exit_code: object = None) -> None:
            return None

        def has_unsettled(self, _identity: str) -> bool:
            return False

    policy = TaskPolicy(grants=frozenset({"control", "project.read"}))
    session = TaskSession(policy=policy)
    snapshot = build_turn_snapshot(session)
    sink = Sink()

    def read_file(call: ToolCall) -> ToolResult:
        return ToolResult(ok=True, call=call, model_text=str(call.args["path"]))

    codemode = build_execute_turn_executor(
        session=session,
        snapshot=snapshot,
        run_id="run-scope",
        effect_scope="outer",
        turn=0,
        executors={"read_file": read_file},
        intent_sink=sink,
    )
    result = execute_turn(
        session,
        [ToolCall("codemode", {"code": "return await Promise.all(['a','b'].map(path => tools.read_file({path})));"})],
        executors={"codemode": codemode},
        run_id="run-scope",
        effect_scope="outer",
        turn=0,
        snapshot=snapshot,
        intent_sink=sink,
    )[0]

    assert result.ok is True
    assert "outer" in sink.scopes
    child_scopes = [scope for scope in sink.scopes if ":codemode:" in scope]
    assert len(child_scopes) == 2
    assert len(set(child_scopes)) == 2
