"""同一 TurnSnapshot 必须贯穿展示、解析、授权、执行器绑定。

注册表在本轮快照构建后发生变化，下一轮才生效：
- normalize_turn 用冻结定义校验（新注册工具本轮不可见）；
- execute_turn 用冻结的执行器绑定（本轮仍执行原绑定）。
"""

from __future__ import annotations

from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import tools_from_specs
from codey.runtime.core.models import ToolCall, ToolResult


def _bind_policy() -> TaskPolicy:
    return TaskPolicy(grants=frozenset({
        "control", "knowledge.read", "knowledge.write", "knowledge.link",
    }))


def test_snapshot_freezes_executor_binding_across_registry_swap():
    from codey.toolchain import tool_spec as spec_module

    calls_a: list[str] = []
    calls_b: list[str] = []

    def fn_a(call):
        calls_a.append(str(getattr(call, "args", {}).get("echo", "")))
        return ToolResult(call, "A-RESULT", ok=True)

    def fn_b(call):
        calls_b.append(str(getattr(call, "args", {}).get("echo", "")))
        return ToolResult(call, "B-RESULT", ok=True)

    assert spec_module.register_custom_tool(
        "snapbind_probe", grant="knowledge.read",
        parameters=(("echo", {"type": "string"}),),
        required=("echo",), description="snapshot binding probe",
    )
    assert spec_module.register_custom_executor("snapbind_probe", fn_a)
    try:
        session = TaskSession(policy=_bind_policy(), task_kind="research", max_turns=2)
        snapshot = build_turn_snapshot(session)
        assert "snapbind_probe" in snapshot.tool_names

        # 本轮快照构建后替换注册项
        assert spec_module.register_custom_executor("snapbind_probe", fn_b)

        # 解析仍用冻结定义执行，不查询实时注册表
        plan = normalize_turn(
            '{"tool": "snapbind_probe", "args": {"echo": "hi"}}',
            snapshot=snapshot,
        )
        assert getattr(plan, "protocol_error", "") == "", f"frozen parse failed: {plan.protocol_error}"
        assert len(plan.calls) == 1

        from codey.operations.kernel_execution import execute_turn

        results = execute_turn(
            session, list(plan.calls),
            run_id="run-snap", effect_scope="snap", turn=1,
            snapshot=snapshot,
        )
        assert calls_a == ["hi"], f"本轮必须执行快照绑定 A，实际 A={calls_a} B={calls_b}"
        assert calls_b == []
        assert "A-RESULT" in str(results[0].model_text)
    finally:
        spec_module.unregister_custom_tool("snapbind_probe")


def test_registry_grant_change_only_affects_the_next_turn():
    from codey.operations.kernel_execution import execute_turn
    from codey.toolchain import tool_spec

    name = "snapshot_grant_probe"
    assert tool_spec.register_custom_tool(name, grant="control")
    assert tool_spec.register_custom_executor(name, lambda call: ToolResult(call, "old binding", ok=True))
    try:
        session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})))
        snapshot = build_turn_snapshot(session)
        assert tool_spec.unregister_custom_tool(name)
        assert tool_spec.register_custom_tool(name, grant="project.write")
        plan = normalize_turn('{"tool":"snapshot_grant_probe","args":{}}', snapshot=snapshot)
        assert not plan.protocol_error
        results = execute_turn(session, plan.calls, snapshot=snapshot)
        assert results[0].model_text == "old binding"
        assert name not in build_turn_snapshot(session).tool_names
    finally:
        tool_spec.unregister_custom_tool(name)


def test_frozen_specs_are_nested_immutable():
    import dataclasses
    from collections.abc import Mapping

    session = TaskSession(policy=_bind_policy(), task_kind="research", max_turns=2)
    snapshot = build_turn_snapshot(session)
    assert snapshot.frozen_specs, "快照必须携带冻结定义"
    for spec in snapshot.frozen_specs:
        for _name, schema in (getattr(spec, "parameters", ()) or ()):
            assert isinstance(schema, Mapping)
            with __import__("pytest").raises(TypeError):
                schema["__frozen_probe__"] = 1
        with __import__("pytest").raises(dataclasses.FrozenInstanceError):
            spec.name = "mutated"  # type: ignore[misc]


def test_snapshot_schema_enum_and_required_are_immutable():
    import pytest

    from codey.toolchain.tool_spec import ToolSpec, freeze_spec_parameters, validate_args_with_spec

    spec = freeze_spec_parameters(ToolSpec(
        name="probe", executor="custom", grant="control",
        parameters=(("data", {"type": "object", "required": ["kind"],
                                "properties": {"kind": {"type": "string", "enum": ["safe"]}},
                                "additionalProperties": False}),), required=("data",),
    ))
    schema = dict(spec.parameters)["data"]
    with pytest.raises((TypeError, AttributeError)):
        schema["required"].append("other")
    with pytest.raises((TypeError, AttributeError)):
        schema["properties"]["kind"]["enum"].append("unsafe")
    assert validate_args_with_spec(spec, {"data": {"kind": "unsafe"}})
    assert validate_args_with_spec(spec, {"data": {}})
    assert not validate_args_with_spec(spec, {"data": {"kind": "safe"}})


def test_native_tools_derive_from_same_frozen_specs():
    session = TaskSession(policy=_bind_policy(), task_kind="research", max_turns=2)
    snapshot = build_turn_snapshot(session, native=True)
    frozen_names = {str(getattr(s, "name", "")) for s in snapshot.frozen_specs}
    schema_names = {
        item.name
        for item in tools_from_specs(snapshot.frozen_specs)
    }
    assert schema_names, "native schema 不得为空"
    assert schema_names <= frozen_names, f"schema 与冻结定义漂移：{schema_names - frozen_names}"


def test_execute_turn_without_snapshot_still_runs():
    from codey.operations.kernel_execution import execute_turn

    session = TaskSession(policy=_bind_policy(), task_kind="research", max_turns=2)
    results = execute_turn(
        session, [ToolCall(name="knowledge_search", args={"query": "q"})],
        executors={"knowledge_search": lambda call: ToolResult(call, "ok", ok=True)},
        run_id="run-nosnap", turn=1,
    )
    assert "ok" in str(results[0].model_text)
