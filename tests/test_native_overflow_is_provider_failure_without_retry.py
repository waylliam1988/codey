"""Native overflow is exactly provider_failure with no retry.

Repro: the old test allowed three stop reasons
(provider_failure/protocol/recovery_failure) while the production contract
is deterministic provider_failure; the strict-ledger test was named
"..._retries_same_batch" while it actually proves "does not retry".

Lock: overflow during native delivery terminates as provider_failure,
exactly one delivery attempt, no text fallback, receipts preserved.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

from codey.operations.task_loop import run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.providers.error_classification import ContextOverflowError
from codey.runtime.core.models import ToolResult


class FakeStructuredProvider:
    name = "local"

    def __init__(self, turns: list[AssistantTurn]) -> None:
        self._turns = list(turns)
        self.tool_results_seen: list = []
        self.sent_prompts: list[str] = []

    def new_chat(self, timeout=None) -> None:
        return None

    def send(self, text: str, timeout=None) -> str:
        raise AssertionError("native path must not use text send()")

    def send_turn(self, prompt: str, tools=None, timeout=None) -> AssistantTurn:
        self.sent_prompts.append(str(prompt))
        return self._turns.pop(0)

    def send_tool_results(self, results, tools=None, timeout=None) -> AssistantTurn:
        self.tool_results_seen.append(list(results))
        return self._turns.pop(0)

    def close(self) -> None:
        return None


def test_native_overflow_is_provider_failure_without_retry(tmp_path: Path) -> None:
    class OverflowProvider(FakeStructuredProvider):
        def __init__(self) -> None:
            super().__init__([AssistantTurn(text="ack")])
            self.fallback_prompts: list[str] = []
            self.tool_sends = 0

        def send_tool_results(self, results, tools=None, timeout=None) -> AssistantTurn:
            self.tool_sends += 1
            raise ContextOverflowError("full")

        def send_turn(self, prompt: str, tools=None, timeout=None) -> AssistantTurn:
            self.fallback_prompts.append(prompt)
            return self._turns.pop(0)

    provider = OverflowProvider()
    (tmp_path / "app.py").write_text("hello\n", encoding="utf-8")
    policy = TaskPolicy(grants=frozenset({"project.read", "control"}))
    session = TaskSession(policy=policy, task_kind="project", project=str(tmp_path), max_turns=3)
    provider._turns = [
        AssistantTurn(text="", tool_calls=(ProviderToolCall(id="c1", name="read_file", arguments={"path": "app.py"}),)),
    ]
    with mock.patch("codey.operations.kernel_transport.provider_uses_native", return_value=True):
        result = run_task_kernel(
            session, provider=provider,
            executors={"read_file": lambda call: ToolResult(call=call, model_text="hello")},
            run_id="r-overflow-lock-1", effect_scope="task",
            provider_id="local", project_path=tmp_path,
            user_task="read app", context_text="",
        )
    assert not result.completed
    assert result.stop_reason == "provider_failure", f"must be deterministic provider_failure: {result}"
    assert provider.tool_sends == 1, f"overflow must not retry delivery, got {provider.tool_sends}"
    assert len(provider.fallback_prompts) == 1, (
        f"overflow must not fall back after failure, got {len(provider.fallback_prompts)} prompts"
    )


def test_strict_ledger_overflow_does_not_retry_same_batch(tmp_path: Path) -> None:
    """Overflow fails closed on the ledger: single NOT_SENT attempt, no retry."""
    from codey.operations.task_effects import KernelEffectSink, KernelRecordedProvider
    from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
    from codey.runtime.log.session_log import RuntimeSessionLog
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    (tmp_path / "app.py").write_text("hello\n", encoding="utf-8")

    class StrictOverflowProvider(FakeStructuredProvider):
        def __init__(self) -> None:
            super().__init__([
                AssistantTurn(text="", tool_calls=(
                    ProviderToolCall(id="c1", name="read_file", arguments={"path": "app.py"}),
                )),
            ])
            self.tool_sends = 0

        def send_turn(self, prompt: str, tools=None, timeout=None) -> AssistantTurn:
            self.sent_prompts.append(str(prompt))
            return self._turns.pop(0)

        def send_tool_results(self, results, tools=None, timeout=None) -> AssistantTurn:
            self.tool_sends += 1
            self.tool_results_seen.append(list(results))
            raise ContextOverflowError("full")

    def _policy() -> TaskPolicy:
        return TaskPolicy(grants=frozenset({"project.read", "control"}))

    session_id, run_id = "sess-native-lock-1", "run-native-lock-1"
    log = RuntimeSessionLog(tmp_path / "state")
    line = RuntimeMutationLine(log)
    line.accept_operation(
        session_id=session_id, run_id=run_id, project=str(tmp_path),
        provider_id="local", turn_budget=10, max_repair_rounds=1, task_kind="project",
    )
    line.mark_writer_running(session_id, run_id, provider_id="local")
    sink = KernelEffectSink(line, session_id=session_id, run_id=run_id, provider_id="local")
    provider = StrictOverflowProvider()
    recorded = KernelRecordedProvider(provider, sink)
    session = TaskSession(policy=_policy(), task_kind="project", project=str(tmp_path), max_turns=3)
    with mock.patch("codey.operations.kernel_transport.provider_uses_native", return_value=True):
        result = run_task_kernel(
            session, provider=recorded,
            executors={"read_file": lambda call: ToolResult(call=call, model_text="hello")},
            run_id=run_id, effect_scope="task", provider_id="local",
            project_path=tmp_path, user_task="read app", context_text="",
            intent_sink=sink,
        )
    assert not result.completed
    assert result.stop_reason == "provider_failure"
    assert provider.tool_sends == 1
    from codey.runtime.effects.effect_records import SENT_STATE_NOT_SENT, RuntimeEffectStore

    effects = RuntimeEffectStore(log).load_effects(session_id, run_id)
    failed = [r for r in effects if r.intent.effect_category == "provider_send" and r.settlement is not None and r.settlement.status == "error"]
    assert failed
    assert failed[-1].settlement is not None
    assert failed[-1].settlement.sent_state == SENT_STATE_NOT_SENT
    batches = ToolResultDeliveryStore(log).load_batches(session_id, run_id)
    assert len(batches) == 1
    batch = batches[0]
    assert len(batch.send_attempts) == 1
    assert not batch.is_delivered
