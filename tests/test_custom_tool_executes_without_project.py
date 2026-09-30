from codey.runtime.core.models import ToolCall


def test_registered_custom_executor_runs_without_project() -> None:
    from codey.operations.kernel_execution import execute_turn
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.toolchain.tool_spec import register_custom_executor, register_custom_tool, unregister_custom_tool

    name = "projectless_custom"
    assert register_custom_tool(name, grant="control", executor="custom", required=())
    assert register_custom_executor(name, lambda call: "ok")
    try:
        session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})), task_kind="chat")
        results = execute_turn(session, [ToolCall(name, {})], run_id="r1", turn=1)
        assert results[0].model_text == "ok"
    finally:
        unregister_custom_tool(name)
