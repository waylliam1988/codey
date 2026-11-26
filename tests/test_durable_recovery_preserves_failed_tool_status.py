"""Actual log/storage reconstruction must retain unsuccessful tool results."""
from contextlib import closing

from codey.durable import DurableSession, SQLiteDurableStore
from codey.operations.task_effects import KernelEffectSink
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.effects.effect_records import RuntimeEffectStore
from tests.support.receipt_fixtures import rebuild_settled_tool_result
from tests.test_settled_delivery_recovery import _accept_and_mark, _new_runtime


def test_real_session_log_restores_failure_without_reexecution(tmp_path):
    mutations, durable = _new_runtime(tmp_path, "s", "r")
    with closing(durable):
        _accept_and_mark(mutations, "s", "r", str(tmp_path))
        sink = KernelEffectSink(mutations, session_id="s", run_id="r", provider_id="local", durable=durable)
        call = ToolCall("read", {"path": "a.txt"}, "c1")
        original = ToolResult(call, "权限不足", ok=False)
        sink.begin_turn([("e1", call, 0)], turn=1)
        sink.settle("e1", False, result=original)

    with closing(DurableSession(SQLiteDurableStore(tmp_path / "state" / "durable.sqlite3"))) as durable:
        projection = RuntimeEffectStore(durable=durable).load_effects("s", "r")[0]
        restored = rebuild_settled_tool_result(projection, session_id="s", run_id="r")
        assert restored.ok is False
        assert restored.call == original.call
        assert restored.model_text == original.model_text
