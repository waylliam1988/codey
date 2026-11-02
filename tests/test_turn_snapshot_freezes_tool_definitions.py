from codey.providers.base import AssistantTurn, ProviderToolCall


def test_tools_registered_after_snapshot_are_not_accepted_this_turn() -> None:
    from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.toolchain.tool_spec import register_custom_tool, unregister_custom_tool

    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})), task_kind="chat")
    snapshot = build_turn_snapshot(session, native=False)
    name = "late_turn_tool"
    assert register_custom_tool(name, grant="control")
    try:
        # 同一 TurnSnapshot 贯穿本轮：注册表变化下一轮生效，本轮仍按冻结快照拒绝
        plan = normalize_turn(
            AssistantTurn(text="", tool_calls=(ProviderToolCall(id="c1", name=name, arguments={}),)),
            snapshot=snapshot,
        )
        assert plan.protocol_error
        assert "snapshot" in plan.protocol_error
    finally:
        unregister_custom_tool(name)
