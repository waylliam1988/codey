"""恢复必须交付原结算结果，而非重新执行产生新观察。

已结算重发的生产链覆盖在 tests/test_settled_delivery_recovery.py
（真实日志重启 → recover_effects_for_resume → 原结果交付）；
本文件保留结算收据字段的单元锁定。
"""
from __future__ import annotations

import tempfile
from pathlib import Path


def _new_sink(tmp: Path, session_id: str, run_id: str):
    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.operations.task_effects import KernelEffectSink
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    durable = DurableSession(SQLiteDurableStore(tmp / f"{session_id}-{run_id}.sqlite3"))
    durable.admit_input(conversation_id=session_id, request_id=f"request-{run_id}", task_id=run_id,
                        text="runtime test", provider_snapshot={"provider_id": "local", "model_id": "test"})
    durable.start_task(run_id, state={"task_kind": "project"})
    durable.advance_task(task_id=run_id, phase="assistant_response_pending", state={"task_kind": "project"})
    mutations = RuntimeMutationLine(durable=durable)
    mutations.accept_operation(
        session_id=session_id, run_id=run_id, project=str(tmp),
        provider_id="local", turn_budget=10, max_repair_rounds=1, task_kind="project",
    )
    mutations.mark_writer_running(session_id, run_id, provider_id="local")
    sink = KernelEffectSink(mutations, session_id=session_id, run_id=run_id, provider_id="local", durable=durable)
    return sink, durable, mutations


def test_settle_persists_exit_code_and_full_bounded_result() -> None:
    from codey.runtime.core.models import ToolCall, ToolResult

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        sink, durable, _ = _new_sink(tmp, "s1", "r1")
        call = ToolCall(name="read_file", args={"path": "a.py"})
        sink.begin_turn([("eff1", call, 0)], turn=1)
        result = ToolResult(ok=True, call=call, model_text="original observation", audit={"exit_code": 0})
        sink.settle("eff1", True, result=result, exit_code=0)
        from codey.runtime.effects.effect_records import RuntimeEffectStore
        store = RuntimeEffectStore(durable=durable)
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
        durable.close()
