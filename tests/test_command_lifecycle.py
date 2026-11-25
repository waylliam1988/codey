"""Automatic waiting and final-result delivery use the production task owner."""
from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from codey.durable.tool_task import execution_effect_id
from codey.operations.generation_boundary import durable_generation_boundary_callback
from codey.operations.kernel_contracts import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelRunRequest,
    KernelTransportDeps,
)
from codey.operations.provider_session import adapt_provider
from codey.operations.task_loop import run_task_kernel
from codey.operations.tool_tasks.delivery import notified_tool_tasks, pending_tool_task_notifications
from codey.providers.base import AssistantTurn, ProviderToolCall, ProviderToolDefinition, ProviderToolResult
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import decode_json_object, require_json_string
from tests.test_tool_task_background import Runtime, open_runtime


@pytest.fixture()
def runtime(tmp_path: Path) -> Iterator[Runtime]:
    opened = open_runtime(tmp_path)
    opened.app.durable_scheduler.start(tool_tasks_only=True)
    try:
        yield opened
    finally:
        assert opened.app.close()


def test_short_command_returns_the_original_execution_result(runtime: Runtime) -> None:
    (runtime.project / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    result = runtime.execute(ToolCall("run", {"command": "python -m pytest -q"}, "short-run"))

    assert result.kind == "completed" and result.ok, result.model_text
    assert "1 passed" in result.model_text
    children = runtime.app.durable.owned_tasks("r")
    assert len(children) == 1
    assert len(runtime.app.durable.effects(children[0].task_id)) == 1
    assert runtime.app.durable.effects(children[0].task_id)[0].effect_id == execution_effect_id(children[0].task_id)
    assert result.call.call_id == "short-run"
    assert result.audit["exit_code"] == 0
    from codey.operations.kernel_prompt import _result_context

    context = _result_context(result, runtime.session)
    assert "Command execution reference:" in context
    assert "Execution facts:" not in context


def test_read_caller_receipt_reaches_original_owned_command_output(runtime: Runtime) -> None:
    (runtime.project / "test_sample.py").write_text(
        "def test_noisy():\n    print('leading ' * 900)\n"
        "    print('ACTUAL_COMMAND_MARKER')\n    print('trailing ' * 900)\n    assert False\n", encoding="utf-8")
    call = ToolCall("run", {"command": "python -m pytest -q -s"})
    caller = runtime.execute(call)
    assert caller.truncated and "ACTUAL_COMMAND_MARKER" not in caller.model_text
    ref = next(identity for identity, result in runtime.session._memory_results.items() if result is caller)
    reply = runtime.execute(ToolCall("read_tool_result",
        {"result_ref": ref, "view": "output", "query": "ACTUAL_COMMAND_MARKER"}), turn=1)
    assert reply.ok, reply.model_text
    payload = decode_json_object(json.loads(reply.model_text))
    assert payload["output_source"] == "execution"
    assert payload["original_output_incomplete"] is False and payload["display_truncated"] is True
    assert "ACTUAL_COMMAND_MARKER" in require_json_string(payload["text"], "text")
    assert len(runtime.app.durable.owned_tasks("r")) == 1


def test_long_command_returns_same_task_and_never_starts_twice(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codey.operations.tool_tasks import execution as tool_tasks

    monkeypatch.setattr(tool_tasks, "COMMAND_YIELD_MS", 10)
    (runtime.project / "test_sample.py").write_text(
        "from pathlib import Path\nimport time\n"
        "def test_slow():\n    p=Path('starts.txt')\n"
        "    p.write_text(p.read_text()+'x' if p.exists() else 'x')\n    time.sleep(0.2)\n", encoding="utf-8")
    call = ToolCall("run", {"command": "python -m pytest -q"}, "long-run")

    accepted = runtime.execute(call)

    assert accepted.kind == "accepted", accepted.model_text
    ref = require_json_string(decode_json_object(accepted.canonical["value"])["task_ref"], "task_ref")
    assert runtime.execute(call).canonical == accepted.canonical
    result = runtime.execute(ToolCall("task_output", {"task_ref": ref, "wait_ms": 30000}), turn=1)
    assert result.ok, result.model_text
    assert decode_json_object(decode_json_object(result.canonical["value"])["result"])["exit_code"] == 0
    assert (runtime.project / "starts.txt").read_text() == "x"
    assert len(runtime.app.durable.effects(ref)) == 1


class CompletionProvider:
    name = "test"

    def __init__(self, native: bool, nested: bool = False) -> None:
        self.native_tools = native
        self.nested = nested
        self.turn = 0
        self.notifications: list[str] = []

    def next_call(self, text: str) -> ToolCall:
        self.turn += 1
        if "Automatic command results from committed receipts:" in text:
            self.notifications.append(text)
        if self.turn == 1:
            if self.nested:
                return ToolCall("codemode", {"code": "return await tools.run({command:'python -m pytest -q'});"})
            return ToolCall("run", {"command": "python -m pytest -q"})
        assert self.turn <= 4, text
        return ToolCall("done", {"summary": "Tests finished."})

    def send(self, text: str) -> str:
        call = self.next_call(text)
        value: dict[str, object] = {"tool": call.name, "args": call.args}
        return json.dumps(value)

    def send_turn(self, prompt: str, tools: Sequence[ProviderToolDefinition]) -> AssistantTurn:
        call = self.next_call(prompt)
        return AssistantTurn(tool_calls=(ProviderToolCall(f"call-{self.turn}", call.name, call.args),))

    def send_tool_results(self, results: Sequence[ProviderToolResult],
                          tools: Sequence[ProviderToolDefinition]) -> AssistantTurn:
        return self.send_turn("\n".join(r.content + "\n" + (r.user_content or "") for r in results), tools)

    def acknowledge_tool_results(self, results: Sequence[ProviderToolResult],
                                 tools: Sequence[ProviderToolDefinition]) -> AssistantTurn:
        return AssistantTurn(text="closed")


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("yield_early", [False, True])
def test_final_result_is_delivered_once_without_model_polling(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, native: bool, nested: bool, yield_early: bool,
) -> None:
    from codey.operations.tool_tasks import execution as tool_tasks

    monkeypatch.setattr(tool_tasks, "COMMAND_YIELD_MS", 1 if yield_early else 10000)
    (runtime.project / "test_sample.py").write_text(
        "import time\ndef test_slow():\n    time.sleep(0.1)\n", encoding="utf-8")
    provider = CompletionProvider(native, nested)

    result = run_task_kernel(runtime.session, request=KernelRunRequest(
        KernelTransportDeps(adapt_provider(provider), run_id="r", provider_id="test", user_task="Run the tests."),
        KernelExecutionDeps(intent_sink=runtime.sink, project_path=runtime.project,
            workspace_revision_store=runtime.app.workspace_revisions),
        KernelObservationDeps(on_boundary=durable_generation_boundary_callback(
            durable=runtime.app.durable, session_id="s", task_id="r", sink=runtime.sink))))

    assert result.completed, result
    assert len(provider.notifications) == int(yield_early)
    if yield_early:
        assert '"outcome": "succeeded"' in provider.notifications[0]
    child = runtime.app.durable.owned_tasks("r")[0]
    assert notified_tool_tasks(runtime.app.durable, owner_task_id="r", store=runtime.sink.managed_outputs) == (
        {child.task_id} if yield_early else set())
    assert pending_tool_task_notifications(runtime.app.durable, owner_task_id="r", store=runtime.sink.managed_outputs,
        results=runtime.session._memory_results).refs == ()
    assert all(r.call.name != "task_output" for r in runtime.session._memory_results.values())
    assert len(runtime.session.verifications) == 1


def test_reserved_caller_recovers_same_execution_after_lost_acknowledgement(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codey.operations.kernel.kernel_context import KernelContext
    from codey.operations.kernel.kernel_driver import _recover_interrupted_effects
    from codey.operations.task_session import TaskSession, turn_effect_id
    from codey.operations.tool_tasks import execution as tool_tasks
    from codey.runtime.core.operation_reducer import ACTION_SYNTHESIZE_INTERRUPTED_EFFECTS, RuntimeAction

    (runtime.project / "test_sample.py").write_text(
        "from pathlib import Path\ndef test_once():\n"
        "    p=Path('starts.txt')\n    p.write_text(p.read_text()+'x' if p.exists() else 'x')\n", encoding="utf-8")

    def lost(*args: object, **kwargs: object) -> ToolResult:
        raise RuntimeError("lost admission acknowledgement")

    with monkeypatch.context() as patch:
        patch.setattr(tool_tasks, "automatic_command_result", lost)
        with pytest.raises(RuntimeError, match="lost admission acknowledgement"):
            runtime.execute(ToolCall("run", {"command": "python -m pytest -q"}))
    origin = turn_effect_id("r", 0, 0)
    assert runtime.app.durable.effect(origin).settlement is None
    restored = TaskSession(runtime.session.policy, project=str(runtime.project))
    context = KernelContext(restored, KernelRunRequest(
        KernelTransportDeps(adapt_provider(CompletionProvider(False)), run_id="r"),
        KernelExecutionDeps(intent_sink=runtime.sink, project_path=runtime.project,
            workspace_revision_store=runtime.app.workspace_revisions)))

    _recover_interrupted_effects(context, RuntimeAction(ACTION_SYNTHESIZE_INTERRUPTED_EFFECTS, effect_ids=(origin,)))
    recovered = runtime.app.durable.effect(origin)
    assert recovered.settlement is not None
    child = runtime.app.durable.owned_tasks("r")[0]
    terminal = tool_tasks.wait_tool_task(runtime.app.durable, child.task_id, owner_task_id="r", wait_ms=30000)
    assert terminal.status == "completed", terminal.state
    context.restore_facts()
    context.restore_facts()
    assert len(restored.verifications) == 1
    assert (runtime.project / "starts.txt").read_text() == "x"
    assert len(runtime.app.durable.owned_tasks("r")) == 1
    assert len(runtime.app.durable.effects(child.task_id)) == 1


def test_large_escaped_output_is_bounded_and_deliverable(runtime: Runtime) -> None:
    from codey.operations.tool_tasks.delivery import NOTIFICATION_MAX_CHARS
    from codey.operations.tool_tasks.execution import wait_tool_task

    (runtime.project / "test_sample.py").write_text(
        "def test_noisy():\n    print('\\x01' * 30000)\n    assert False\n", encoding="utf-8")
    ref = runtime.accept("python -m pytest -q -s")
    assert wait_tool_task(runtime.app.durable, ref, owner_task_id="r", wait_ms=30000).status == "completed"
    notification = pending_tool_task_notifications(runtime.app.durable, owner_task_id="r",
        store=runtime.sink.managed_outputs, results={})
    assert notification.refs == (ref,)
    assert len(notification.text) <= NOTIFICATION_MAX_CHARS
    assert '"outcome": "failed"' in notification.text


@pytest.mark.parametrize(("status", "code", "expected"), [
    ("exit", 0, "succeeded"), ("exit", 1, "failed"), ("timeout", None, "timed_out"),
    ("output_drain_timeout", 0, "incomplete"), ("output_read_error", 0, "incomplete"),
    ("cancelled", None, "cancelled"),
])
def test_automatic_terminal_outcomes(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, status: str, code: int | None, expected: str,
) -> None:
    from codey.operations.tool_tasks import execution as tool_tasks
    from codey.runtime.core.execution_receipt import ExecutionReceipt
    from codey.toolchain.runtime import ToolOutcome

    receipt = ExecutionReceipt(status, code, "available output", exit_code_scope="process")
    release = threading.Event()

    def raw(*args: object, **kwargs: object) -> ToolOutcome:
        if status == "cancelled":
            assert release.wait(5)
        return ToolOutcome(receipt.render(), ok=status == "exit" and code == 0,
            audit=receipt.audit_facts(), canonical={"execution": receipt.facts()})

    monkeypatch.setattr(tool_tasks, "run_command_raw", raw)
    ref = runtime.accept()
    if status == "cancelled":
        runtime.app.durable.request_cancel(ref)
        release.set()
    child = tool_tasks.wait_tool_task(runtime.app.durable, ref, owner_task_id="r", wait_ms=30000)
    assert child.status in {"completed", "cancelled"}, child.state
    notification = pending_tool_task_notifications(runtime.app.durable, owner_task_id="r",
        store=runtime.sink.managed_outputs, results={})
    assert f'"outcome": "{expected}"' in notification.text


@pytest.mark.parametrize("not_sent", [False, True])
@pytest.mark.parametrize("native", [False, True])
def test_notification_failure_distinguishes_unsent_from_uncertain_delivery(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, not_sent: bool, native: bool,
) -> None:
    from codey.operations.tool_tasks import execution as tool_tasks
    from codey.providers.error_classification import RequestPrepError

    class FailingProvider(CompletionProvider):
        def next_call(self, text: str) -> ToolCall:
            if "Automatic command results from committed receipts:" in text:
                if not_sent:
                    raise RequestPrepError("injected before submission")
                raise RuntimeError("injected uncertain submission")
            return super().next_call(text)

    monkeypatch.setattr(tool_tasks, "COMMAND_YIELD_MS", 1)
    (runtime.project / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    result = run_task_kernel(runtime.session, request=KernelRunRequest(
        KernelTransportDeps(adapt_provider(FailingProvider(native)), run_id="r", provider_id="test"),
        KernelExecutionDeps(intent_sink=runtime.sink, project_path=runtime.project,
            workspace_revision_store=runtime.app.workspace_revisions),
        KernelObservationDeps(on_boundary=durable_generation_boundary_callback(
            durable=runtime.app.durable, session_id="s", task_id="r", sink=runtime.sink))))

    assert not result.completed
    child = runtime.app.durable.owned_tasks("r")[0]
    assert child.status == "completed"
    delivered = notified_tool_tasks(runtime.app.durable, owner_task_id="r", store=runtime.sink.managed_outputs)
    assert delivered == (set() if not_sent else {child.task_id})
    pending = pending_tool_task_notifications(runtime.app.durable, owner_task_id="r",
        store=runtime.sink.managed_outputs, results={})
    assert pending.refs == ((child.task_id,) if not_sent else ())


def test_legacy_admission_restores_original_args_and_current_version_detaches_inputs() -> None:
    from codey.durable.tool_task import ToolTaskInput
    from codey.types.json_value import JsonObject

    args: JsonObject = {"command": "python -m pytest -q"}
    request = ToolTaskInput("origin", "run", args, {}, "project", "coding_writer", 1, 0)
    payload = request.to_payload()
    args["command"] = "changed"
    assert ToolTaskInput.from_payload(payload).args["command"] == "python -m pytest -q"
    payload["schema_version"] = 1
    payload.pop("resource_binding")
    legacy = ToolTaskInput.from_payload(payload)
    assert legacy.args == {"command": "python -m pytest -q", "run_in_background": True}
    payload["args"] = legacy.args
    with pytest.raises(ValueError, match="legacy tool task"):
        ToolTaskInput.from_payload(payload)
