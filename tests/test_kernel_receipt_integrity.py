"""Durable delivery must preserve the observation, not just its preview."""

from codey.operations.recovery import rebuild_settled_tool_result
from codey.operations.task_effects import KernelEffectSink
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.effects.effect_records import RuntimeEffectStore
from codey.storage.managed_outputs import ManagedOutputStore
from tests.test_settled_delivery_recovery import _accept_and_mark, _new_log


def test_long_model_result_and_metadata_survive_restart(tmp_path):
    log, mutations = _new_log(tmp_path)
    _accept_and_mark(mutations, "session", "run", str(tmp_path))
    store = ManagedOutputStore(tmp_path / "state")
    sink = KernelEffectSink(mutations, session_id="session", run_id="run",
                            provider_id="local", managed_outputs=store)
    call = ToolCall("run", {"command": "pytest", "path": "."}, call_id="call-1")
    result = ToolResult(call, "完整输出\r\n" * 1800, ok=True,
                        canonical={"ok": True}, audit={"exit_code": 0, "capture_truncated": False},
                        presentation={"summary": "验证通过"})
    sink.begin_turn([("effect-1", call, 0)], turn=1)
    sink.settle("effect-1", True, result=result)
    log, _ = _new_log(tmp_path)
    projection = RuntimeEffectStore(log).load_effects("session", "run")[0]
    restored = rebuild_settled_tool_result(projection, managed_store=store,
                                          session_id="session", run_id="run")
    assert restored is not None
    assert restored.model_text == result.model_text
    assert restored.call == call
    assert restored.canonical == result.canonical
    assert restored.presentation == result.presentation
    assert restored.audit == result.audit
    assert restored.truncated == result.truncated


def test_managed_receipt_preserves_crlf_bytes(tmp_path):
    store = ManagedOutputStore(tmp_path)
    text = "first\r\nsecond\r\n"
    ref = store.write_tool_output(session_id="s", run_id="r", tool_id="t",
                                  permission_profile="coding_writer", tool_name="run",
                                  display_ref="test", text=text)
    assert ref is not None
    restored, _ = store.read_tool_output("s", "r", ref.handle)
    assert restored == text
