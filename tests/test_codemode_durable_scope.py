from __future__ import annotations

from collections.abc import Iterable
from contextlib import closing

from codey.durable import DurableSession
from codey.runtime.effects.effect_records import RuntimeEffectIntent
from codey.runtime.effects.tool_result_delivery import DeliveryBatchIntent
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.runtime.write.tool_batches import ToolBatchCommit
from tests.support.sqlite_store import TemporarySQLiteStore
from tests.test_tool_task_background import Runtime
from tests.test_tool_task_background import runtime as runtime


def test_codemode_child_scopes_produce_distinct_durable_batches() -> None:
    from codey.operations.task_effects import KernelEffectSink
    from codey.runtime.core.models import ToolCall

    class Mutations(RuntimeMutationLine):
        def __init__(self, durable: DurableSession) -> None:
            super().__init__(durable=durable)
            self.batch_ids: list[str] = []

        def begin_tool_batch(self, session_id: str, run_id: str, *, intents: Iterable[RuntimeEffectIntent],
                             delivery_intent: DeliveryBatchIntent | None, driver: str = "") -> ToolBatchCommit:
            assert delivery_intent is not None
            self.batch_ids.append(delivery_intent.batch_id)
            return ToolBatchCommit(delivery_intent.batch_id, tuple(intent.effect_id for intent in intents), driver)

    with closing(DurableSession(TemporarySQLiteStore())) as durable:
        mutations = Mutations(durable)
        sink = KernelEffectSink(mutations, session_id="s", run_id="r", provider_id="p", durable=durable)
        sink.begin_turn([("r:child:0", ToolCall("read_file", {"path": "a.py"}), 0)], turn=0, scope="cm:child:0")
        sink.begin_turn([("r:child:1", ToolCall("read_file", {"path": "b.py"}), 1)], turn=0, scope="cm:child:1")

    assert len(mutations.batch_ids) == 2
    assert mutations.batch_ids[0] != mutations.batch_ids[1]


def test_nested_execute_turn_persists_distinct_child_effects(runtime: Runtime) -> None:
    from codey.codemode.kernel import build_execute_turn_executor
    from codey.operations.kernel.kernel_execution import execute_turn
    from codey.runtime.core.models import ToolCall

    for name in ("a", "b"):
        (runtime.project / name).write_text("observed\n", encoding="utf-8")
    executor = build_execute_turn_executor(session=runtime.session, snapshot=runtime.snapshot,
        run_id="r", session_id="s", effect_scope="outer", turn=0, project_path=runtime.project,
        intent_sink=runtime.sink, durable=runtime.app.durable, managed_outputs=runtime.app.managed_outputs)
    result, = execute_turn(runtime.session, [ToolCall("codemode", {
        "code": "return await Promise.all(['a','b'].map(path => tools.read_file({path})));",
    })], run_id="r", session_id="s", effect_scope="outer", turn=0, project_path=runtime.project,
        snapshot=runtime.snapshot, executors={"codemode": executor}, intent_sink=runtime.sink,
        durable=runtime.app.durable, managed_outputs=runtime.app.managed_outputs)
    assert result.ok, result.model_text
    effects = runtime.app.durable.effects("r")
    parent, = [effect for effect in effects if effect.intent.get("tool_name") == "codemode"]
    children = [effect for effect in effects if effect.intent.get("parent_effect_id") == parent.effect_id]
    assert len(children) == 2
    assert len({effect.effect_id for effect in children}) == 2
    assert all(effect.intent.get("tool_name") == "read" and effect.status == "ok" for effect in children)
