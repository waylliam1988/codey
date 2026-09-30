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
        plan = normalize_turn(
            AssistantTurn(text="", tool_calls=(ProviderToolCall(id="c1", name=name, arguments={}),)),
            policy=session.policy,
            controller_allowed=snapshot.allowed,
        )
        assert plan.protocol_error
        assert "snapshot" in plan.protocol_error
    finally:
        unregister_custom_tool(name)
