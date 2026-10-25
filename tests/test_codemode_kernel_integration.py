from __future__ import annotations

import json

from codey.policies.task_policy import TaskPolicy


def test_codemode_is_visible_in_every_read_capable_snapshot() -> None:
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.task_session import TaskSession

    policy = TaskPolicy(grants=frozenset({"control", "project.read"}))
    snapshot = build_turn_snapshot(TaskSession(policy=policy))
    assert "codemode" in snapshot.tool_names
    assert '"code"' in snapshot.contract_text


def test_text_and_native_protocols_both_normalize_codemode() -> None:
    from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
    from codey.operations.task_session import TaskSession
    from codey.protocols.text_turn import decode_text_turn
    from codey.providers.base import AssistantTurn, ProviderToolCall

    policy = TaskPolicy(grants=frozenset({"control", "project.read"}))
    session = TaskSession(policy=policy)
    snapshot = build_turn_snapshot(session)
    text_plan = normalize_turn(
        decode_text_turn('{"tool":"codemode","args":{"code":"return 1"}}'),
        snapshot=snapshot,
    )
    native_plan = normalize_turn(
        AssistantTurn(tool_calls=(ProviderToolCall("c1", "codemode", {"code": "return 1"}),)),
        snapshot=snapshot,
    )
    assert text_plan.calls[0].name == "codemode"
    assert native_plan.calls[0].name == "codemode"


def test_codemode_kernel_executor_routes_children_through_callback() -> None:
    from codey.codemode.kernel import CodeModeKernelExecutor
    from codey.runtime.core.models import ToolCall, ToolResult

    children: list[ToolCall] = []

    def execute_child(call: ToolCall) -> ToolResult:
        children.append(call)
        return ToolResult(ok=True, call=call, model_text=f"contents:{call.args['path']}")

    executor = CodeModeKernelExecutor(
        execution_id="run-1:turn:0:slot:0",
        catalog=(
            {"name": "read_file", "description": "read", "parameters": {"type": "object"}, "read_only": True,
             "output_schema": {"type": "string"}},
        ),
        execute_child=execute_child,
    )
    result = executor.execute(ToolCall("codemode", {"code": "return await tools.read_file({path:'a.py'});"}))

    assert result.ok is True
    assert children[0].name == "read_file"
    assert children[0].call_id == "run-1:turn:0:slot:0:child:0"
    assert "contents:a.py" in json.dumps(result.canonical, ensure_ascii=False)


def test_nested_child_reenters_execute_turn_with_frozen_snapshot() -> None:
    from codey.codemode.kernel import CodeModeKernelExecutor
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.task_session import TaskSession
    from codey.runtime.core.models import ToolCall, ToolResult

    policy = TaskPolicy(grants=frozenset({"control", "project.read"}))
    session = TaskSession(policy=policy)
    snapshot = build_turn_snapshot(session)

    def read_file(call: ToolCall) -> ToolResult:
        return ToolResult(ok=True, call=call, model_text=f"read:{call.args['path']}")

    def child(call: ToolCall) -> ToolResult:
        return execute_turn(
            session,
            [call],
            executors={"read_file": read_file},
            run_id="run-1",
            effect_scope="codemode-parent",
            turn=0,
            tool_index_base=0,
            snapshot=snapshot,
        )[0]

    executor = CodeModeKernelExecutor(
        execution_id="run-1:turn:0:slot:0",
        catalog=({"name": "read_file", "description": "read", "parameters": {"type": "object"}, "read_only": True,
                  "output_schema": {"type": "string"}},),
        execute_child=child,
    )
    result = execute_turn(
        session,
        [ToolCall("codemode", {"code": "return await tools.read_file({path:'a.py'});"})],
        executors={"codemode": executor.execute},
        run_id="run-1",
        effect_scope="outer",
        turn=0,
        snapshot=snapshot,
    )[0]

    assert result.ok is True
    assert "read:a.py" in result.model_text


def test_production_builder_wires_nested_calls_into_execute_turn() -> None:
    from codey.codemode.kernel import build_execute_turn_executor
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.kernel_protocol import build_turn_snapshot
    from codey.operations.task_session import TaskSession
    from codey.runtime.core.models import ToolCall, ToolResult

    policy = TaskPolicy(grants=frozenset({"control", "project.read"}))
    session = TaskSession(policy=policy)
    snapshot = build_turn_snapshot(session)

    def read_file(call: ToolCall) -> ToolResult:
        from codey.toolchain.runtime import _format_read_file_page

        text = f"kernel:{call.args['path']}"
        page = _format_read_file_page(call.args['path'], 1, 300, 1, [text])
        return ToolResult(ok=True, call=call, model_text=page.model_text, canonical=page.canonical)

    codemode = build_execute_turn_executor(
        session=session,
        snapshot=snapshot,
        run_id="run-2",
        effect_scope="outer",
        turn=0,
        executors={"read_file": read_file},
    )
    result = execute_turn(
        session,
        [ToolCall("codemode", {"code": "return await tools.read_file({path:'b.py'});"})],
        executors={"codemode": codemode},
        run_id="run-2",
        effect_scope="outer",
        turn=0,
        snapshot=snapshot,
    )[0]

    assert result.ok is True
    assert "kernel:b.py" in result.model_text
