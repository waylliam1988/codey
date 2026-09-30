"""恢复必须交付原结算结果，而非重新执行产生新观察。"""
from __future__ import annotations

import tempfile
from pathlib import Path


def _new_sink(tmp: Path, session_id: str, run_id: str):
    from codey.operations.task_effects import KernelEffectSink
    from codey.runtime.log.session_log import RuntimeSessionLog
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    log = RuntimeSessionLog(tmp / f"{session_id}-{run_id}.log")
    mutations = RuntimeMutationLine(log)
    mutations.accept_operation(
        session_id=session_id, run_id=run_id, project=str(tmp),
        provider_id="local", turn_budget=10, max_repair_rounds=1, task_kind="project",
    )
    mutations.mark_writer_running(session_id, run_id, provider_id="local")
    sink = KernelEffectSink(mutations, session_id=session_id, run_id=run_id, provider_id="local")
    return sink, log, mutations


def test_settle_persists_exit_code_and_full_bounded_result() -> None:
    from codey.runtime.core.models import ToolCall, ToolResult

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        sink, log, _ = _new_sink(tmp, "s1", "r1")
        call = ToolCall(name="read_file", args={"path": "a.py"})
        sink.begin_turn([("eff1", call, 0)], turn=1)
        result = ToolResult(call=call, model_text="original observation", audit={"exit_code": 0})
        sink.settle("eff1", True, result=result, exit_code=0)
        from codey.runtime.effects.effect_records import RuntimeEffectStore
        store = RuntimeEffectStore(log)
        projs = store.load_effects("s1", "r1")
        assert len(projs) == 1
        st = projs[0].settlement
        assert st is not None
        # 必须持久化 exit_code，且 excerpt/ref 之外可重建原结果
        payload = st.to_payload()
        assert payload.get("exit_code") == 0, f"settlement 未持久化 exit_code: {payload}"
        # 短结果必须保存完整有界结果（不只是 500 截断的展示）
        full = payload.get("result_text", payload.get("result_excerpt", ""))
        assert "original observation" in str(full)


def test_settled_read_is_redelivered_not_reread() -> None:
    """已结算的读取在恢复时直接交付原结果，不重新读文件。"""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        proj = tmp / "proj"
        proj.mkdir()
        (proj / "a.py").write_text("original observation", encoding="utf-8")
        from codey.runtime.core.models import ToolCall, ToolResult
        sink, log, _ = _new_sink(tmp, "s2", "r2")
        call = ToolCall(name="read_file", args={"path": "a.py"})
        from codey.operations.task_session import turn_effect_id
        eff = turn_effect_id("r2:task", 1, 0)
        sink.begin_turn([(eff, call, 0)], turn=1)
        sink.settle(eff, True, result=ToolResult(call=call, model_text="original observation"), exit_code=0)
        # 文件变化
        (proj / "a.py").write_text("changed after original execution", encoding="utf-8")
        from codey.runtime.effects.effect_records import RuntimeEffectStore
        store = RuntimeEffectStore(log)
        projs = store.load_effects("s2", "r2")
        settled = [p for p in projs if p.is_settled]
        assert settled, "需要已结算投影"
        st = settled[0].settlement
        assert st is not None
        # settlement 必须可重建原结果
        full = getattr(st, "result_text", "") or st.result_excerpt
        assert "original observation" in str(full)
        content_now = (proj / "a.py").read_text(encoding="utf-8")
        assert content_now == "changed after original execution"
        assert str(full) != content_now
        # 恢复消费链：从 settlement 重建 ToolResult，必须等于原结果而非当前文件
        from codey.operations.recovery import rebuild_settled_tool_result
        rebuilt = rebuild_settled_tool_result(settled[0], call)
        assert rebuilt is not None
        assert "original observation" in str(getattr(rebuilt, "model_text", ""))
