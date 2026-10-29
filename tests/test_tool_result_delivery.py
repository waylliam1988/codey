"""Delivery and restart use the real operation driver and its original facts."""
from dataclasses import replace

import pytest

from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_loop import run_task_kernel
from codey.providers.api_transport import GenerationUnknownError
from codey.runtime.effects.tool_result_delivery import ToolResultDeliveryStore
from tests.support.kernel_restart import paused_batch


def restart_request(runtime, provider=None):
    original = runtime.sink
    sink = KernelEffectSink(original.mutations, session_id=original.session_id, run_id=original.run_id,
        provider_id=original.provider_id, durable=original.durable, managed_outputs=original.managed_outputs)
    from codey.operations.generation_boundary import durable_generation_boundary_callback
    return replace(runtime.request,
        transport=replace(runtime.request.transport, provider=provider or runtime.request.transport.provider),
        observation=replace(runtime.request.observation, intent_sink=sink,
            on_boundary=durable_generation_boundary_callback(durable=sink.durable, session_id=sink.session_id,
                                                             task_id=sink.run_id, sink=sink)))


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("boundary", ["intent", "receipt"])
def test_restart_retains_original_batch_and_delivers_once(tmp_path, native, boundary):
    with paused_batch(tmp_path, native=native, pause_at=boundary) as runtime:
        original = next(effect for effect in runtime.durable.effects("r") if effect.category == "tool_call")
        assert len(runtime.executions) == (boundary == "receipt")
        result = run_task_kernel(runtime.session(), request=restart_request(runtime))
        assert result.completed, result
        assert len(runtime.executions) == 1
        receipt = runtime.durable.effect(original.effect_id).settlement
        assert receipt.get("replay_count", 0) == (boundary == "intent")
        if boundary == "intent":
            assert receipt["replayed_from_effect_id"] == original.effect_id
        batches = ToolResultDeliveryStore(durable=runtime.durable).load_batches("s", "r")
        assert len(batches) == 1 and batches[0].is_delivered
        assert batches[0].intent.tool_refs == (original.effect_id,)
        if native:
            assert [rows[0][0] for kind, rows in runtime.events if kind == "results"] == ["original-read"]
        snapshot = (runtime.durable.task_facts("r"), list(runtime.events))
        repeated = run_task_kernel(runtime.session(), request=restart_request(runtime))
        assert repeated == result
        assert (runtime.durable.task_facts("r"), runtime.events) == snapshot


def test_unknown_provider_intent_is_never_resent_or_replayed(tmp_path):
    with paused_batch(tmp_path, pause_at="provider_intent") as runtime:
        assert not runtime.events and not runtime.executions
        result = run_task_kernel(runtime.session(), request=restart_request(runtime))
        assert result.stop_reason == "recovery_failure"
        assert not runtime.events and not runtime.executions
        effects = runtime.durable.effects("r")
        assert len(effects) == 1
        assert effects[0].settlement["sent_state"] == "maybe_sent"
        second = run_task_kernel(runtime.session(), request=restart_request(runtime))
        assert second.stop_reason == "recovery_failure"
        assert runtime.durable.effects("r") == effects


def test_unknown_delivery_retains_result_and_refuses_another_send(tmp_path):
    with paused_batch(tmp_path, native=True) as runtime:
        sent = []
        class Provider(type(runtime.request.transport.provider)):
            def send_tool_results(self, results, tools):
                sent.append([row.call_id for row in results])
                raise GenerationUnknownError("connection lost after submission")
        request = restart_request(runtime, Provider())
        original = next(effect for effect in runtime.durable.effects("r") if effect.category == "tool_call")
        result = run_task_kernel(runtime.session(), request=request)
        assert not result.completed
        assert sent == [["original-read"]]
        assert len(runtime.executions) == 1
        assert runtime.durable.effect(original.effect_id) == original
        repeated = run_task_kernel(runtime.session(), request=restart_request(runtime, Provider()))
        assert repeated.stop_reason == "recovery_failure"
        assert sent == [["original-read"]]
        assert not ToolResultDeliveryStore(durable=runtime.durable).load_batches("s", "r")[0].is_delivered


def test_failed_delivery_admission_never_calls_provider(tmp_path, monkeypatch):
    with paused_batch(tmp_path, native=True) as runtime:
        request = restart_request(runtime)
        def unavailable(*args, **kwargs):
            raise OSError("transaction unavailable")
        monkeypatch.setattr(runtime.sink.mutations, "admit_provider_request", unavailable)
        before = list(runtime.events)
        result = run_task_kernel(runtime.session(), request=request)
        assert not result.completed
        assert runtime.events == before and len(runtime.executions) == 1
        batch = ToolResultDeliveryStore(durable=runtime.durable).load_batches("s", "r")[0]
        assert not batch.send_attempts and not batch.is_delivered


def test_result_prompt_keeps_original_observation(tmp_path):
    with paused_batch(tmp_path) as runtime:
        result = run_task_kernel(runtime.session(), request=restart_request(runtime))
        assert result.completed
        continuation = [text for kind, text in runtime.events if kind == "send"][-1]
        assert "ORIGINAL_RESULT" in continuation
        assert "ORIGINAL_GOAL" in continuation
        assert "Current work observations" in continuation
