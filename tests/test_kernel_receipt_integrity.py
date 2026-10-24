"""Durable delivery must preserve the observation, not just its preview."""

from codey.operations.recovery import rebuild_settled_tool_result
from codey.operations.task_effects import KernelEffectSink
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.effects.effect_records import RuntimeEffectStore
from codey.storage.managed_outputs import ManagedOutputStore
from tests.test_settled_delivery_recovery import _accept_and_mark, _new_log


def test_long_model_result_and_metadata_survive_restart(tmp_path):
    log, mutations, durable = _new_log(tmp_path, "session", "run")
    _accept_and_mark(mutations, "session", "run", str(tmp_path))
    store = ManagedOutputStore(tmp_path / "state")
    sink = KernelEffectSink(mutations, session_id="session", run_id="run",
                            provider_id="local", managed_outputs=store, durable=durable)
    call = ToolCall("run", {"command": "pytest", "path": "."}, call_id="call-1")
    result = ToolResult(call, "完整输出\r\n" * 1800, ok=True,
                        canonical={"ok": True}, audit={"exit_code": 0, "capture_truncated": False},
                        presentation={"summary": "验证通过"})
    sink.begin_turn([("effect-1", call, 0)], turn=1)
    sink.settle("effect-1", True, result=result)
    projection = RuntimeEffectStore(durable=durable).load_effects("session", "run")[0]
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


def test_large_source_observation_survives_real_result_receipt_and_restart(tmp_path):
    from codey.operations.kernel_facts import record_facts_for_result
    from codey.operations.task_session import TaskSession
    from codey.research.ledger import ResearchLedger
    from codey.research.ledger_receipts import ledger_observation, restore_ledger_observation

    _, mutations, durable = _new_log(tmp_path, "session", "run")
    _accept_and_mark(mutations, "session", "run", str(tmp_path))
    store = ManagedOutputStore(tmp_path / "state")
    sink = KernelEffectSink(mutations, session_id="session", run_id="run",
                            provider_id="local", managed_outputs=store, durable=durable)
    url = "https://example.com/source?identity=" + "x" * 2500
    body = "exact source\r\n中文内容\r\n" * 3000
    ledger = ResearchLedger()
    ledger.record_open(url, url, "Large source", body)
    observation = ledger_observation(ledger, "open", url=url)
    call = ToolCall("open_url", {"url": url}, call_id="source-call")
    result = ToolResult(call, "bounded preview", ok=True, canonical={"research_observation": observation})
    assert result.canonical["research_observation"]["text"] == body
    sink.begin_turn([("source-effect", call, 0)], turn=1)
    sink.settle("source-effect", True, result=result)
    projection = RuntimeEffectStore(durable=durable).load_effects("session", "run")[0]
    restored = rebuild_settled_tool_result(projection, managed_store=store, session_id="session", run_id="run")
    assert restored is not None and restored.canonical == result.canonical
    fresh, restored_ledger = TaskSession(policy=None), ResearchLedger()
    record_facts_for_result(fresh, restored.call, restored, ok=True)
    restore_ledger_observation(restored_ledger, restored.canonical["research_observation"])
    assert fresh.opened_sources == {url}
    assert restored_ledger.source_text_for_url(url) == body
    assert restored_ledger.opened_sources == ledger.opened_sources
