"""Known truncation is recoverable without accepting incomplete operations."""
from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from codey.durable import DurableSession, SQLiteDurableStore
from codey.durable.model import CommitResult, DurableMutation
from codey.operations.generation_boundary import durable_generation_boundary_callback
from codey.operations.provider_session import adapt_provider
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_loop import (
    KernelExecutionDeps,
    KernelObservationDeps,
    KernelRunRequest,
    KernelTransportDeps,
    run_task_kernel,
)
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import RESEARCH_ANSWER_REQUIRED_CHECKS, TaskPolicy
from codey.providers.api_provider import ApiProvider
from codey.providers.base import AssistantTurn, TurnFinish
from codey.providers.error_classification import OutputLengthError
from codey.providers.requests import ProviderRequest
from codey.providers.transport import ProviderTransport
from codey.runtime.write.checkpoint import operation_from_checkpoint
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.storage.managed_outputs import ManagedOutputStore
from tests.support.sqlite_store import TemporarySQLiteStore


@dataclass(frozen=True)
class Runtime:
    durable: DurableSession
    sink: KernelEffectSink


@pytest.fixture()
def runtime(tmp_path: Path) -> Iterator[Runtime]:
    durable = DurableSession(TemporarySQLiteStore())
    durable.admit_input(conversation_id="s", request_id="input", task_id="r", text="Check the task", provider_snapshot={})
    durable.claim_task("r")
    durable.start_task("r", state={})
    line = RuntimeMutationLine(durable=durable)
    line.accept_operation(session_id="s", run_id="r", provider_id="local", turn_budget=4,
                          max_repair_rounds=1, task_kind="project")
    line.mark_writer_running("s", "r", provider_id="local")
    sink = KernelEffectSink(line, session_id="s", run_id="r", provider_id="local", durable=durable,
                            managed_outputs=ManagedOutputStore(tmp_path))
    try:
        yield Runtime(durable, sink)
    finally:
        durable.close()


def response(protocol: str, text: str, *, limited: bool) -> dict[str, object]:
    if protocol == "openai-completions":
        return {"choices": [{"finish_reason": "length" if limited else "stop",
                              "message": {"role": "assistant", "content": text}}]}
    return {"status": "incomplete" if limited else "completed",
            "incomplete_details": {"reason": "max_output_tokens"} if limited else None,
            "output": [{"type": "message", "role": "assistant",
                        "content": [{"type": "output_text", "text": text}]}]}


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
@pytest.mark.parametrize("always_limited", [False, True])
def test_text_api_truncation_uses_one_durable_continuation(
        runtime: Runtime, monkeypatch: pytest.MonkeyPatch, protocol: str, always_limited: bool) -> None:
    provider = ApiProvider("http://fixture.invalid/v1", "fixture", api_protocol=protocol, native_tools=False)
    prompts: list[object] = []

    def generate(messages: object, *args: object, **kwargs: object) -> dict[str, object]:
        prompts.append(messages)
        limited = len(prompts) == 1 or always_limited
        # A syntactically complete call in a length-limited response is still incomplete work.
        return response(protocol, '{"tool":"done","args":{"summary":"finished"}}', limited=limited)

    monkeypatch.setattr(provider, "_generate", generate)
    session = TaskSession(TaskPolicy(frozenset({"control"})), max_turns=4)
    result = run_task_kernel(session, request=KernelRunRequest(
        KernelTransportDeps(adapt_provider(provider), run_id="r", provider_id="local", user_task="Check the task"),
        KernelExecutionDeps(intent_sink=runtime.sink),
        KernelObservationDeps(on_boundary=durable_generation_boundary_callback(
            durable=runtime.durable, session_id="s", task_id="r", sink=runtime.sink))))
    assert len(prompts) == 2, result
    assert "truncated" in str(prompts[1])
    assert result.completed is not always_limited, result
    assert result.stop_reason == ("provider_failure" if always_limited else "done")
    assert runtime.sink.counters().length_continuation_used
    effects = [effect for effect in runtime.durable.effects("r") if effect.category == "provider_send"]
    assert len(effects) == 2
    assert all(runtime.durable.artifacts_for_effect(effect.effect_id) for effect in effects)
    # No length-limited call can become an executed done operation.
    assert not always_limited or not session.executed


@pytest.mark.parametrize("protocol", ["openai-completions", "openai-responses"])
def test_plain_send_remains_strict_and_never_commits_partial_text(
        monkeypatch: pytest.MonkeyPatch, protocol: str) -> None:
    provider = ApiProvider("http://fixture.invalid/v1", "fixture", api_protocol=protocol)

    def generate(*args: object, **kwargs: object) -> dict[str, object]:
        return response(protocol, "incomplete answer", limited=True)

    monkeypatch.setattr(provider, "_generate", generate)
    with pytest.raises(OutputLengthError):
        provider.send("question")
    history: object = provider._messages
    assert history == [{"role": "user", "content": "question"}]


@pytest.mark.parametrize("external_edit", [False, True])
def test_symbol_recovery_requests_current_source_or_a_new_read(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, external_edit: bool,
) -> None:
    provider = ApiProvider("http://fixture.invalid/v1", "fixture", native_tools=False, thinking_enabled=False,
                           text_tool_format="json_schema")
    project = tmp_path / "project"
    project.mkdir()
    target = project / "example.py"
    target.write_text("def value(): return 0\n", encoding="utf-8")
    requests: list[str] = []

    def generate(messages: object, *args: object, **kwargs: object) -> dict[str, object]:
        requests.append("\n".join(str(part) for part in (
            messages, kwargs.get("tool_call_contract", ""), kwargs.get("tool_call_observations", ""))))
        turn = len(requests)
        if turn == 1 or (turn == 3 and external_edit):
            text = '{"tool":"read_file","args":{"path":"example.py"}}'
        elif turn == 2:
            if external_edit:
                target.write_text("def value(): return 99\n", encoding="utf-8")
            text = '{"tool":"edit","args":{"path":"example.py","replacements":['
        elif turn == 3:
            text = "def value(): return 1"
        else:
            text = '{"tool":"done","args":{"summary":"Finished"}}'
        return response("openai-completions", text, limited=turn == 2)

    monkeypatch.setattr(provider, "_generate", generate)
    session = TaskSession(TaskPolicy(frozenset({"control", "project.read", "project.write"})),
                          task_kind="project", project=str(project), max_turns=4)
    run_task_kernel(session, request=KernelRunRequest(
        KernelTransportDeps(adapt_provider(provider), run_id="r", provider_id="local", user_task="Update example.py"),
        KernelExecutionDeps(intent_sink=runtime.sink, project_path=project),
        KernelObservationDeps(on_boundary=durable_generation_boundary_callback(
            durable=runtime.durable, session_id="s", task_id="r", sink=runtime.sink))))
    retry = requests[2]
    if external_edit:
        assert "Current definition:" not in retry
        assert "Read example.py again" in retry
        assert "def value(): return 99" in requests[3]
        assert target.read_text(encoding="utf-8") == "def value(): return 99\n"
    else:
        assert "Current definition:\ndef value(): return 0" in retry
        assert target.read_text(encoding="utf-8") == "def value(): return 1\n"


class LimitedTransport(ProviderTransport):
    provider: object = None
    native_tools = False

    def __init__(self, *, known: bool = True) -> None:
        self.known = known
        self.calls = 0
        self.requests: list[ProviderRequest] = []

    def execute_request(self, request: ProviderRequest) -> object:
        self.calls += 1
        self.requests.append(request)
        if self.known:
            return AssistantTurn(text='{"tool":"run","args":{"command":"unsafe"}}',
                                 finish=TurnFinish.OUTPUT_LIMIT)
        raise TimeoutError("unknown network outcome")


@pytest.mark.parametrize("known", [False, True])
def test_unknown_outcome_and_persisted_retry_budget_do_not_resend(
        runtime: Runtime, known: bool) -> None:
    provider = LimitedTransport(known=known)
    request = KernelRunRequest(KernelTransportDeps(provider, run_id="r", provider_id="local"),
            KernelExecutionDeps(intent_sink=runtime.sink),
            KernelObservationDeps(on_boundary=durable_generation_boundary_callback(
                durable=runtime.durable, session_id="s", task_id="r", sink=runtime.sink)))
    result = run_task_kernel(TaskSession(TaskPolicy(frozenset({"control"})), max_turns=4), request=request)
    assert not result.completed
    assert result.stop_reason == "provider_failure"
    assert provider.calls == (2 if known else 1)
    if known:
        again = run_task_kernel(TaskSession(TaskPolicy(frozenset({"control"})), max_turns=4), request=request)
        assert again == result
        assert provider.calls == 2
        assert runtime.sink.counters().length_continuation_used


def test_research_truncation_keeps_its_submission_contract(runtime: Runtime) -> None:
    from codey.providers.requests import SendTextRequest

    provider = LimitedTransport()
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "knowledge.read", "knowledge.write"}),
        strict_research=True, required_checks=RESEARCH_ANSWER_REQUIRED_CHECKS), task_kind="research", max_turns=4)
    result = run_task_kernel(session, request=KernelRunRequest(
        KernelTransportDeps(provider, run_id="r", provider_id="local"),
        KernelExecutionDeps(intent_sink=runtime.sink),
        KernelObservationDeps(on_boundary=durable_generation_boundary_callback(
            durable=runtime.durable, session_id="s", task_id="r", sink=runtime.sink))))
    assert result.stop_reason == "provider_failure" and provider.calls == 2
    continuation = provider.requests[1]
    assert isinstance(continuation, SendTextRequest)
    assert "submit_research_answer for the complete task" in continuation.prompt
    assert "done(summary)" not in continuation.prompt
    assert not session.executed


def test_cancellation_after_truncation_prevents_continuation(runtime: Runtime) -> None:
    stop = threading.Event()

    class StoppedTransport(LimitedTransport):
        def execute_request(self, request: ProviderRequest) -> object:
            reply = super().execute_request(request)
            stop.set()
            return reply

    provider = StoppedTransport()
    result = run_task_kernel(TaskSession(TaskPolicy(frozenset({"control"})), max_turns=4),
        request=KernelRunRequest(KernelTransportDeps(provider, run_id="r", provider_id="local", stop_flag=stop),
            KernelExecutionDeps(intent_sink=runtime.sink),
            KernelObservationDeps(on_boundary=durable_generation_boundary_callback(
                durable=runtime.durable, session_id="s", task_id="r", sink=runtime.sink))))
    assert not result.completed and result.stop_reason == "stopped"
    assert provider.calls == 1


def test_cold_restart_cannot_reset_text_continuation_budget(tmp_path: Path) -> None:
    class SimulatedExit(BaseException):
        pass

    def opened(*, resume: bool) -> Runtime:
        durable = DurableSession(SQLiteDurableStore(tmp_path / "runtime.sqlite"))
        line = RuntimeMutationLine(durable=durable)
        if resume:
            durable.recover_scheduler_state(force=True)
            if durable.task("r").status == "pending":
                durable.claim_task("r")
        else:
            durable.admit_input(conversation_id="s", request_id="input", task_id="r", text="Check", provider_snapshot={})
            durable.claim_task("r")
            durable.start_task("r", state={})
            line.accept_operation(session_id="s", run_id="r", provider_id="local", turn_budget=4,
                                  max_repair_rounds=1, task_kind="project")
            line.mark_writer_running("s", "r", provider_id="local")
        return Runtime(durable, KernelEffectSink(line, session_id="s", run_id="r", provider_id="local",
                       durable=durable, managed_outputs=ManagedOutputStore(tmp_path)))

    def request_for(current: Runtime, provider: LimitedTransport) -> KernelRunRequest:
        return KernelRunRequest(KernelTransportDeps(provider, run_id="r", provider_id="local"),
            KernelExecutionDeps(intent_sink=current.sink),
            KernelObservationDeps(on_boundary=durable_generation_boundary_callback(
                durable=current.durable, session_id="s", task_id="r", sink=current.sink)))

    original = opened(resume=False)

    def committed(result: CommitResult, mutation: DurableMutation) -> None:
        state = operation_from_checkpoint(original.durable.task("r"), "s")
        if state is not None and state.leaf == "control_ready" and state.counters.length_continuation_used:
            raise SimulatedExit()

    original.durable.subscribe_commits(committed)
    first = LimitedTransport()
    try:
        with pytest.raises(SimulatedExit):
            run_task_kernel(TaskSession(TaskPolicy(frozenset({"control"})), max_turns=4),
                request=request_for(original, first))
        assert first.calls == 1
    finally:
        original.durable.close()
    resumed = opened(resume=True)
    second = LimitedTransport()
    try:
        result = run_task_kernel(TaskSession(TaskPolicy(frozenset({"control"})), max_turns=4),
            request=request_for(resumed, second))
        assert result.stop_reason == "provider_failure" and result.turns == 2
        assert second.calls == 1 and resumed.sink.counters().length_continuation_used
    finally:
        resumed.durable.close()
