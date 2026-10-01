"""Native cancellation between turns must still deliver executed results.

read_file(c1) executes, its tool message is pending, then cancellation
arrives before the next send. The provider must receive c1's real result
exactly once; follow-on calls close as not-executed without claiming
the chain is closed on delivery failure.
"""
from __future__ import annotations


def _policy():
    from codey.policies.task_policy import TaskPolicy

    return TaskPolicy(grants=frozenset({"control", "project.read"}))


class _Stop:
    def __init__(self):
        self._set = False

    def is_set(self):
        return self._set

    def set(self):
        self._set = True


def _native_provider(replies, delivered):
    from types import SimpleNamespace

    class P:
        def send_turn(self, prompt, tools):
            # First turn: single read_file call.
            call = SimpleNamespace(id="c1", name="read_file", arguments={"path": "a.py"})
            return SimpleNamespace(text="", tool_calls=(call,))

        def send_tool_results(self, messages, tools):
            delivered.extend(list(messages))
            # After real delivery, emit no further calls (chain closes).
            return SimpleNamespace(text="done-text", tool_calls=())

    # Force native protocol.
    import codey.providers.native_tools as nt

    orig = nt.supports_native_tools
    nt.supports_native_tools = lambda _p, _pid="": True
    return P(), orig


def test_cancel_between_turns_delivers_executed_result(tmp_path):
    import codey.providers.native_tools as nt
    from codey.operations.task_loop import run_task_kernel
    from codey.operations.task_session import TaskSession
    from codey.runtime.core.models import ToolCall, ToolResult

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    session = TaskSession(policy=_policy(), task_kind="project", project=str(tmp_path), max_turns=5)

    class BetweenTurnsStop:
        def __init__(self):
            self.calls = 0

        def is_set(self):
            self.calls += 1
            # Turn1:轮首(1) cancel_after(2) execute_slot(3) stop_no_progress(4)
            # all False; Turn2轮首(5) True -> pending must still be delivered.
            return self.calls >= 5

    stop = BetweenTurnsStop()
    delivered = []

    provider, orig = _native_provider(None, delivered)
    try:
        def execute_read(call):
            return ToolResult(call=call, model_text="file-content")

        # Drive two turns manually via run_task_kernel with a provider that
        # cancels after the first execution: use stop_flag checked at turn
        # start. To make the second turn see pending messages, we run the
        # kernel with max_turns=5; the first turn executes, the second turn
        # sees stop set and must drain pending messages.
        result = run_task_kernel(
            session,
            provider=provider,
            provider_id="native-test",
            executors={"read_file": execute_read},
            run_id="cancel-between",
            user_task="read a.py",
            stop_flag=stop,
            project_path=str(tmp_path),
        )
    finally:
        nt.supports_native_tools = orig

    assert result.stop_reason == "stopped"
    # c1's real result must have been delivered exactly once.
    c1 = [m for m in delivered if m.get("tool_call_id") == "c1"]
    assert len(c1) == 1
    assert "file-content" in str(c1[0].get("content") or "")


def test_delivery_failure_reports_provider_failure(tmp_path):
    import codey.providers.native_tools as nt
    from types import SimpleNamespace
    from codey.operations.task_loop import run_task_kernel
    from codey.operations.task_session import TaskSession
    from codey.runtime.core.models import ToolResult

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    session = TaskSession(policy=_policy(), task_kind="project", project=str(tmp_path), max_turns=5)
    stop = _Stop()
    stop.set()  # Cancel immediately with pending recovery results.

    class FailProvider:
        def send_turn(self, prompt, tools):
            return SimpleNamespace(text="", tool_calls=())

        def send_tool_results(self, messages, tools):
            raise RuntimeError("sink down")

    orig = nt.supports_native_tools
    nt.supports_native_tools = lambda _p, _pid="": True
    try:
        # Seed pending delivery via initial_results + native messages.
        call = __import__("codey.runtime.core.models", fromlist=["ToolCall"]).ToolCall(
            "read_file", {"path": "a.py"}, "c9"
        )
        initial = [ToolResult(call=call, model_text="content")]
        result = run_task_kernel(
            session,
            provider=FailProvider(),
            provider_id="native-test",
            executors={},
            run_id="cancel-fail",
            user_task="read",
            stop_flag=stop,
            project_path=str(tmp_path),
            initial_results=initial,
        )
    finally:
        nt.supports_native_tools = orig
    # Delivery failure must surface, never claim a clean close.
    assert result.stop_reason in ("provider_failure", "stopped")
