"""已结算结果必须经生产恢复入口重发原结果，不得重新执行工具。

覆盖 run（不可重放）/read（可重放）/knowledge_write 在“已结算、未交付、
重启”后的真实链路：RuntimeSessionLog 重开 → recover_effects_for_resume →
原结果交付、原 native call id 应答、执行次数不增加。
直接调用结果重建 helper 的测试不能替代本链路。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace


def _new_log(tmp: Path):
    from codey.runtime.log.session_log import RuntimeSessionLog
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    log = RuntimeSessionLog(tmp / "sesslog")
    mutations = RuntimeMutationLine(log)
    return log, mutations


def _accept_and_mark(mutations, session_id: str, run_id: str, project: str):
    mutations.accept_operation(
        session_id=session_id, run_id=run_id, project=project,
        provider_id="local", turn_budget=10, max_repair_rounds=1, task_kind="project",
    )
    mutations.mark_writer_running(session_id, run_id, provider_id="local")


def _settle_batch(tmp: Path, session_id: str, run_id: str, *, long_text: str = ""):
    """真实执行一次并结算（run 不可重放），交付前中断。返回日志目录。"""
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.task_effects import KernelEffectSink
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.runtime.core.models import ToolCall, ToolResult
    from codey.storage.managed_outputs import ManagedOutputStore

    log, mutations = _new_log(tmp)
    _accept_and_mark(mutations, session_id, run_id, str(tmp))
    sink = KernelEffectSink(
        mutations, session_id=session_id, run_id=run_id, provider_id="local",
        managed_outputs=ManagedOutputStore(tmp / "state"),
    )
    run_call = ToolCall(name="run", args={"command": "python -m pytest", "path": "."}, call_id="native-run-1")
    read_text = long_text or "original observation"
    read_call = ToolCall(name="read_file", args={"path": "a.py"}, call_id="native-read-2")
    def execute_run(call):
        marker = tmp / "execution-count.txt"
        count = int(marker.read_text()) if marker.exists() else 0
        marker.write_text(str(count + 1))
        return ToolResult(call, "run output hi", ok=True, audit={"exit_code": 0})

    if long_text:
        store = ManagedOutputStore(tmp / "state")
        ref = store.write_tool_output(
            session_id=session_id, run_id=run_id, tool_id="1:1",
            permission_profile="coding_writer", tool_name="read_file",
            display_ref="a.py", text=long_text,
        )
        assert ref is not None
        audit = {"managed_output": {
            "handle": ref.handle, "original_bytes": ref.original_bytes,
            "stored_bytes": ref.stored_bytes, "sha256": ref.sha256,
            "original_sha256": ref.original_sha256,
            "stored_truncated": ref.stored_truncated,
        }}
        result = ToolResult(ok=True, call=read_call, model_text=long_text[:2000], audit=audit, truncated=True)
    else:
        result = ToolResult(ok=True, call=read_call, model_text=read_text)
    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control", "project.read", "project.verify"})),
                          project=str(tmp))
    results = execute_turn(session, [run_call, read_call], run_id=run_id, turn=1,
                           project_path=tmp, intent_sink=sink, snapshot=build_turn_snapshot(session),
                           executors={"run":execute_run, "read_file":lambda _:result})
    assert results[0].model_text == "run output hi"
    assert (tmp / "execution-count.txt").read_text() == "1"
    return long_text


def _recover(tmp: Path, session_id: str, run_id: str, project: str):
    from codey.operations.recovery import recover_effects_for_resume
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
    from codey.runtime.log.session_log import RuntimeSessionLog
    from codey.runtime.write.mutation_line import RuntimeMutationLine
    from codey.storage.managed_outputs import ManagedOutputStore

    # 销毁运行对象，重新打开日志（真实重启）
    log = RuntimeSessionLog(tmp / "sesslog")
    mutations = RuntimeMutationLine(log)
    deps = SimpleNamespace(
        runtime_mutations=mutations,
        runtime_effects=RuntimeEffectStore(log),
        tool_result_delivery=ToolResultDeliveryStore(log),
        managed_outputs=ManagedOutputStore(tmp / "state"),
    )
    return recover_effects_for_resume(
        deps, session_id=session_id, run_id=run_id, project=project, task_kind="project",
    )


def test_settled_run_redelivered_without_reexecution_via_production_recovery():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        _settle_batch(tmp, "s-settled", "r-settled")
        from unittest.mock import patch

        with patch("codey.operations.recovery.execute_information_tool_call",
                   side_effect=AssertionError("settled tools must not re-execute")), \
             patch("codey.operations.kernel_execution.execute_turn",
                   side_effect=AssertionError("settled tools must not re-enter execution")):
            recovery = _recover(tmp, "s-settled", "r-settled", str(tmp))
        assert recovery.ok is True, "已结算 run 必须恢复交付"
        assert len(recovery.recovered_tool_outcomes) == 2, (
            f"run+read 两条已结算结果都应交付，实际 {len(recovery.recovered_tool_outcomes)}"
        )
        by_index = {rec.tool_index: rec for rec in recovery.recovered_tool_outcomes}
        run_rec, read_rec = by_index[0], by_index[1]
        assert "run output hi" in str(run_rec.outcome.model_text)
        assert run_rec.outcome.exit_code == 0
        assert run_rec.call.call_id == "native-run-1", "原 native call id 必须保留"
        assert read_rec.call.call_id == "native-read-2"
        assert "original observation" in str(read_rec.outcome.model_text)
        assert run_rec.redelivered is True and read_rec.redelivered is True
        assert (tmp / "execution-count.txt").read_text() == "1", "实际执行次数必须仍为 1"


def test_settled_source_window_is_restored_without_substituting_full_body():
    long_text = "L0123456789\n" * 900  # 约 9900 字符
    assert len(long_text) > 8000
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        _settle_batch(tmp, "s-long", "r-long", long_text=long_text)
        recovery = _recover(tmp, "s-long", "r-long", str(tmp))
        assert recovery.ok is True
        by_index = {rec.tool_index: rec for rec in recovery.recovered_tool_outcomes}
        read_rec = by_index[1]
        assert read_rec.call.call_id == "native-read-2"
        # 受管收据完好时恢复不得丢内容、不得谎称未截断
        assert read_rec.outcome.model_text.startswith(long_text[:2000])
        assert long_text not in read_rec.outcome.model_text
        assert read_rec.outcome.truncated is True


def test_long_result_cannot_settle_without_durable_receipt():
    """Receipt persistence failure leaves the effect uncertain, never clipped success."""
    import pytest

    from codey.operations.task_effects import KernelEffectSink
    from codey.runtime.core.models import ToolCall, ToolResult
    from codey.runtime.effects.effect_records import RuntimeEffectStore
    from codey.runtime.log.session_log import RuntimeSessionLog
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        log = RuntimeSessionLog(tmp / "sesslog")
        mutations = RuntimeMutationLine(log)
        mutations.accept_operation(
            session_id="s-honest", run_id="r-honest", project=str(tmp),
            provider_id="local", turn_budget=10, max_repair_rounds=1, task_kind="project",
        )
        mutations.mark_writer_running("s-honest", "r-honest", provider_id="local")
        sink = KernelEffectSink(
            mutations, session_id="s-honest", run_id="r-honest", provider_id="local",
        )
        full = "X0123456789\n" * 900
        assert len(full) > 8000
        call = ToolCall(name="read_file", args={"path": "a.py"})
        sink.begin_turn([("eff-h", call, 0)], turn=1)
        with pytest.raises(ValueError, match="durable managed receipt"):
            sink.settle("eff-h", True, result=ToolResult(ok=True, call=call, model_text=full))
        assert RuntimeEffectStore(log).load_effects("s-honest", "r-honest")[0].is_pending


def test_settled_redelivery_with_missing_receipt_fails_closed():
    long_text = "M0123456789\n" * 900
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        _settle_batch(tmp, "s-miss", "r-miss", long_text=long_text)
        # 删除受管收据
        for path in (tmp / "state").rglob("*"):
            if path.is_file():
                path.unlink()
        recovery = _recover(tmp, "s-miss", "r-miss", str(tmp))
        assert recovery.ok is False, "收据缺失必须明确恢复失败，不得静默截断交付"


def test_settled_redelivery_with_tampered_receipt_fails_closed():
    long_text = "T0123456789\n" * 900
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        _settle_batch(tmp, "s-tamp", "r-tamp", long_text=long_text)
        for path in (tmp / "state").rglob("*.txt"):
            path.write_text("tampered", encoding="utf-8")
        recovery = _recover(tmp, "s-tamp", "r-tamp", str(tmp))
        assert recovery.ok is False, "收据被改必须明确恢复失败"
