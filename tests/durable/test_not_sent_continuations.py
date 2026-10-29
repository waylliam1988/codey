"""Known unsent deliveries resume their original work through the sole driver."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from codey.durable import DurableSession, SQLiteDurableStore
from codey.durable.checkpoint import checkpoint_entries, operation_from_checkpoint
from codey.operations import completion_gate
from codey.operations.kernel_driver import load_prepared_request, prepare_provider_request
from codey.operations.provider_receipts import REQUEST_KIND, read_provider_artifact
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_loop import run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall, ProviderToolResult
from codey.providers.error_classification import RequestPrepError
from codey.runtime.core.models import ToolResult
from codey.runtime.core.operation_payload import RuntimeExecutionCounters
from codey.runtime.core.operation_state import RuntimeOperationTransitionError
from codey.runtime.core.protocol_window import ProtocolWindowRef, ProviderBinding
from codey.runtime.effects.effect_records import RuntimeEffectError
from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
from codey.runtime.log.session_projection import reduce_session
from codey.runtime.log.session_view import load_session_view
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.runtime.write.provider_effects import build_provider_begin_rows
from tests.durable.test_operation_control_actions import request_for
from tests.durable.test_provider_response_receipts import runtime
from tests.support.kernel_restart import SimulatedExit

RESTARTS = ["same_sink", "new_sink", "sqlite_reopen"]
POLICY = TaskPolicy(frozenset({"control", "project.read"}))


class BoundProvider:
    native_tools = True

    def __init__(self):
        self.original_window = self.window_ref = ProtocolWindowRef.new(
            "s", ProviderBinding("local", "1", "model", "native", "history"))

    def restore_protocol_window(self, window):
        assert window == self.original_window
        self.window_ref = window

    def cold_window(self):
        self.window_ref = ProtocolWindowRef.new("s", self.original_window.binding)

    def send_tool_results_bound(self, results, window, tools):
        assert window == self.original_window == self.window_ref
        return self.send_tool_results(results, tools)

    def acknowledge_tool_results_bound(self, results, window, tools):
        assert window == self.original_window == self.window_ref
        return self.acknowledge_tool_results(results, tools)


def restart(root, durable, outputs, sink, mode):
    if mode == "same_sink":
        return durable, sink
    if mode == "sqlite_reopen":
        durable.close()
        durable = DurableSession(SQLiteDurableStore(root / "runtime.sqlite"))
        durable.recover_scheduler_state(force=True)
        if durable.task("r").status == "pending":
            durable.claim_task("r")
    line = RuntimeMutationLine(durable=durable)
    if mode == "sqlite_reopen":
        line.mark_writer_running("s", "r", provider_id="local")
    return durable, KernelEffectSink(line, session_id="s", run_id="r",
        provider_id="local", durable=durable, managed_outputs=outputs)


def run(durable, sink, provider, *, executors=None):
    request = request_for(durable, sink, provider, executors=executors)
    request = replace(request, observation=replace(request.observation, propagate_provider_failure=False))
    return run_task_kernel(TaskSession(POLICY, max_turns=4), request=request)


@pytest.mark.parametrize("mode", RESTARTS)
@pytest.mark.parametrize("ok", [False, True])
def test_known_not_sent_tool_results_resume_original_batch_twice(tmp_path, mode, ok):
    durable, _, outputs, sink = runtime(tmp_path, sqlite=mode == "sqlite_reopen")
    reads, deliveries, generations = [], [], []

    class Provider(BoundProvider):
        def send_turn(self, prompt, tools):
            generations.append(prompt)
            return AssistantTurn(tool_calls=(ProviderToolCall("original-read", "read_file", {"path": "a.py"}),))

        def send_tool_results(self, results, tools):
            deliveries.append([(row.call_id, row.content) for row in results])
            if len(deliveries) <= 2:
                raise RequestPrepError("not submitted")
            return AssistantTurn(tool_calls=(ProviderToolCall("done", "done", {"summary": "reported"}),))

        def acknowledge_tool_results(self, results, tools):
            return AssistantTurn()

    def read(call):
        reads.append(call)
        return ToolResult(call, "ORIGINAL_RESULT" if ok else "ORIGINAL_FAILURE", ok=ok)

    provider = Provider()
    try:
        for _ in range(2):
            result = run(durable, sink, provider, executors={"read_file": read})
            assert not result.completed, result
            state = operation_from_checkpoint(durable.task("r"), "s")
            assert state.leaf == "tool_delivery_pending", result
            assert state.turn == state.logical_turn == 1
            durable, sink = restart(tmp_path, durable, outputs, sink, mode)
            if mode != "same_sink":
                provider.cold_window()
        result = run(durable, sink, provider, executors={"read_file": read})
        assert result.completed and result.turns == 2, result
        assert len(generations) == len(reads) == 1
        assert len(deliveries) == 3 and deliveries[0] == deliveries[1] == deliveries[2]
        assert deliveries[0][0][0] == "original-read"
        assert ("ORIGINAL_RESULT" if ok else "ORIGINAL_FAILURE") in deliveries[0][0][1]
        batches = ToolResultDeliveryStore(durable=durable).load_batches("s", "r")
        assert len(batches) == 1
        batch = batches[0]
        assert batch.is_delivered and len(batch.send_attempts) == 3
        assert batch.superseded_effect_ids == batch.send_attempts[:2]
        assert batch.delivered_effect_ids == batch.active_attempts == batch.send_attempts[2:]
        effects = durable.effects("r")
        assert len([effect for effect in effects if effect.category == "tool_call"]) == 1
        assert all(effect.settlement is not None for effect in effects)
        history = durable.entries("s")
        durable, sink = restart(tmp_path, durable, outputs, sink, mode)
        assert run(durable, sink, provider, executors={"read_file": read}) == result
        assert durable.entries("s") == history
        assert len(deliveries) == 3 and len(reads) == len(generations) == 1
    finally:
        durable.close()


@pytest.mark.parametrize("mode", RESTARTS)
@pytest.mark.parametrize("control", ["protocol", "done_rejected"])
def test_known_not_sent_control_continuation_preserves_original_decision(tmp_path, monkeypatch, mode, control):
    durable, _, outputs, sink = runtime(tmp_path, sqlite=mode == "sqlite_reopen")
    generations, deliveries, evaluations = [], [], []
    evaluate = completion_gate.evaluate

    def gate(*args, **kwargs):
        evaluations.append(True)
        if control == "done_rejected" and len(evaluations) == 1:
            return SimpleNamespace(complete=False, followup="Continue checking", proof=None, final_text="")
        return evaluate(*args, **kwargs)

    monkeypatch.setattr(completion_gate, "evaluate", gate)

    class Provider(BoundProvider):
        def send_turn(self, prompt, tools):
            generations.append(prompt)
            return AssistantTurn(tool_calls=(ProviderToolCall("original-control",
                "unknown_tool" if control == "protocol" else "done", {"summary": "proposed"}),))

        def send_tool_results(self, results, tools):
            deliveries.append(list(results))
            if len(deliveries) <= 2:
                raise RequestPrepError("not submitted")
            return AssistantTurn(tool_calls=(ProviderToolCall("done", "done", {"summary": "finished"}),))

        def acknowledge_tool_results(self, results, tools):
            return AssistantTurn()

    provider = Provider()
    try:
        for _ in range(2):
            result = run(durable, sink, provider)
            assert not result.completed and result.stop_reason != "recovery_failure", result
            state = operation_from_checkpoint(durable.task("r"), "s")
            assert state.leaf == "control_ready"
            failed = durable.effects("r")[-1]
            payload = read_provider_artifact(outputs, durable.artifact(failed.intent["request_ref"]),
                kind=REQUEST_KIND, session_id="s", run_id="r", effect_id=failed.effect_id)
            assert payload["work_ref"] == state.work_ref
            assert payload["source_response_ref"]
            durable, sink = restart(tmp_path, durable, outputs, sink, mode)
            if mode != "same_sink":
                provider.cold_window()
        result = run(durable, sink, provider)
        assert result.completed and result.turns == 2, result
        assert len(generations) == 1
        assert len(deliveries) == 3 and deliveries[0] == deliveries[1] == deliveries[2]
        assert deliveries[0][0].call_id == "original-control"
        assert deliveries[0][0].content.startswith("ERROR:")
        assert len(evaluations) == (2 if control == "done_rejected" else 1)
        assert all(effect.settlement is not None for effect in durable.effects("r"))
    finally:
        durable.close()


@pytest.mark.parametrize("control", ["results", "protocol", "done_rejected"])
def test_text_continuation_survives_repeated_not_sent_sqlite_reopen(tmp_path, monkeypatch, control):
    durable, _, outputs, sink = runtime(tmp_path, sqlite=True)
    prompts, reads, evaluations = [], [], []
    evaluate = completion_gate.evaluate

    def gate(*args, **kwargs):
        evaluations.append(True)
        if control == "done_rejected" and len(evaluations) == 1:
            return SimpleNamespace(complete=False, followup="Continue checking", proof=None, final_text="")
        return evaluate(*args, **kwargs)

    monkeypatch.setattr(completion_gate, "evaluate", gate)

    class Provider(BoundProvider):
        native_tools = False

        def send(self, prompt):
            assert self.window_ref == self.original_window
            prompts.append(prompt)
            if len(prompts) == 1:
                return {"results": '{"tool":"read_file","args":{"path":"a.py"}}',
                    "protocol": '{"tool":"unknown_tool","args":{}}',
                    "done_rejected": '{"tool":"done","args":{"summary":"proposed"}}'}[control]
            if len(prompts) <= 3:
                raise RequestPrepError("not submitted")
            return '{"tool":"done","args":{"summary":"finished"}}'

    def read(call):
        reads.append(call)
        return ToolResult(call, "ORIGINAL_RESULT", ok=True)

    provider = Provider()
    try:
        for _ in range(2):
            result = run(durable, sink, provider, executors={"read_file": read})
            assert result.stop_reason == "provider_failure", result
            durable, sink = restart(tmp_path, durable, outputs, sink, "sqlite_reopen")
            provider.cold_window()
        result = run(durable, sink, provider, executors={"read_file": read})
        assert result.completed and result.turns == 2, result
        assert len(prompts) == 4
        assert len(reads) == (1 if control == "results" else 0)
        if control == "results":
            assert all("ORIGINAL_RESULT" in prompt for prompt in prompts[1:])
            [batch] = ToolResultDeliveryStore(durable=durable).load_batches("s", "r")
            assert batch.is_delivered and len(batch.superseded_effect_ids) == 2
        assert len(evaluations) == (2 if control == "done_rejected" else 1)
    finally:
        durable.close()


@pytest.mark.parametrize("mode", RESTARTS)
@pytest.mark.parametrize("control", [False, True])
def test_uncertain_delivery_is_never_superseded_or_resent(tmp_path, mode, control):
    durable, _, outputs, sink = runtime(tmp_path, sqlite=mode == "sqlite_reopen")
    generations, deliveries, reads = [], [], []

    class Provider(BoundProvider):
        def send_turn(self, prompt, tools):
            generations.append(prompt)
            return AssistantTurn(tool_calls=(ProviderToolCall("original",
                "unknown_tool" if control else "read_file", {"path": "a.py"}),))

        def send_tool_results(self, results, tools):
            deliveries.append(results)
            raise TimeoutError("submission outcome unknown")

    def read(call):
        reads.append(call)
        return ToolResult(call, "ORIGINAL_RESULT", ok=True)

    provider = Provider()
    try:
        assert not run(durable, sink, provider, executors={"read_file": read}).completed
        before = durable.task_facts("r")
        for _ in range(2):
            durable, sink = restart(tmp_path, durable, outputs, sink, mode)
            result = run(durable, sink, provider, executors={"read_file": read})
            assert result.stop_reason == "recovery_failure", result
            assert "external_outcome_unknown" in result.summary
        assert durable.effects("r") == before[1]
        assert len(generations) == len(deliveries) == 1
        assert len(reads) == (0 if control else 1)
        if not control:
            [batch] = ToolResultDeliveryStore(durable=durable).load_batches("s", "r")
            assert not batch.is_terminal and len(batch.active_attempts) == 1
            assert not batch.superseded_effect_ids
    finally:
        durable.close()


def test_exit_after_superseding_unsent_attempt_does_not_grant_a_second_send(tmp_path):
    durable, _, outputs, sink = runtime(tmp_path, sqlite=True)
    reads, deliveries = [], []

    class Provider:
        native_tools = True

        def send_turn(self, prompt, tools):
            return AssistantTurn(tool_calls=(ProviderToolCall("original", "read_file", {"path": "a.py"}),))

        def send_tool_results(self, results, tools):
            deliveries.append(results)
            raise RequestPrepError("not submitted")

    def read(call):
        reads.append(call)
        return ToolResult(call, "ORIGINAL_RESULT", ok=True)

    provider = Provider()

    def exit_after_admission(_result, mutation):
        if mutation.effects:
            raise SimulatedExit()

    try:
        assert not run(durable, sink, provider, executors={"read_file": read}).completed
        durable.subscribe_commits(exit_after_admission)
        with pytest.raises(SimulatedExit):
            run(durable, sink, provider, executors={"read_file": read})
        [batch] = ToolResultDeliveryStore(durable=durable).load_batches("s", "r")
        assert len(batch.send_attempts) == 2 and batch.superseded_effect_ids == batch.send_attempts[:1]
        before = durable.effects("r")
        durable, sink = restart(tmp_path, durable, outputs, sink, "sqlite_reopen")
        for _ in range(2):
            result = run(durable, sink, provider, executors={"read_file": read})
            assert result.stop_reason == "recovery_failure", result
        assert len(reads) == len(deliveries) == 1
        effects = durable.effects("r")
        assert effects[:-1] == before[:-1]
        assert effects[-1].intent == before[-1].intent
        assert effects[-1].settlement["error_code"] == "interrupted_by_crash"
        assert ToolResultDeliveryStore(durable=durable).load_batches("s", "r") == (batch,)
    finally:
        durable.close()


@pytest.mark.parametrize("invalid", ["unsettled", "maybe_sent", "ok", "wrong_batch", "wrong_category"])
def test_prepared_admission_requires_proof_for_the_same_original_batch(tmp_path, invalid):
    durable, _, _, sink = runtime(tmp_path)

    class Provider:
        native_tools = True

        def send_turn(self, prompt, tools):
            return AssistantTurn(tool_calls=(ProviderToolCall("original", "read_file", {"path": "a.py"}),))

        def send_tool_results(self, results, tools):
            raise RequestPrepError("not submitted")

    def read(call):
        return ToolResult(call, "ORIGINAL_RESULT", ok=True)

    try:
        assert not run(durable, sink, Provider(), executors={"read_file": read}).completed
        ref = prepare_provider_request(sink, "send_tool_results", ([ProviderToolResult("original", "ORIGINAL_RESULT")], []),
                                       {}, counters=None)
        payload, intent = load_prepared_request(sink, ref)
        task, effects = durable.task_facts("r")
        entries = checkpoint_entries(task, effects, session_id="s")
        view = load_session_view(entries, session_id="s", run_id="r")
        failed = next(effect for effect in view.effects if effect.intent.effect_id == effects[-1].effect_id)
        altered = {
            "unsettled": replace(failed, settlement=None),
            "maybe_sent": replace(failed, settlement=replace(failed.settlement, sent_state="maybe_sent")),
            "ok": replace(failed, settlement=replace(failed.settlement, status="ok")),
            "wrong_batch": replace(failed, intent=replace(failed.intent, delivery_batch_id="another-batch")),
            "wrong_category": replace(failed, intent=replace(failed.intent, effect_category="tool_call", request_ref="")),
        }[invalid]
        invalid_view = replace(view, effects=tuple(altered if effect == failed else effect for effect in view.effects))
        admission = payload["admission"]
        with pytest.raises((RuntimeEffectError, RuntimeOperationTransitionError),
                           match="prepared request|NOT_SENT|another delivery batch|provider send effect"):
            build_provider_begin_rows(reduce_session(entries), invalid_view, session_id="s", run_id="r", intent=intent,
                delivery_batch_id=intent.delivery_batch_id, expected_request_ref=ref,
                expected_counters=RuntimeExecutionCounters.from_payload(admission["expected_counters"]),
                counters=RuntimeExecutionCounters.from_payload(admission["counters"]))
        assert durable.task_facts("r") == (task, effects)
    finally:
        durable.close()
