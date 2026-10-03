"""Observe real kernel turns without changing protocol or cancellation."""

from types import SimpleNamespace

from codey.operations.task_loop import KernelObservationDeps, KernelRunRequest, KernelTransportDeps, run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy


def test_cooperative_provider_cancellation_is_a_stopped_task():
    from codey.runtime.core.cancellation import TaskCancelled

    class Provider:
        def send(self, text):
            raise TaskCancelled("user stopped")

    session = TaskSession(policy=TaskPolicy(grants=frozenset({"control"})))
    outcome = run_task_kernel(
        session,
        request=KernelRunRequest(
            transport=KernelTransportDeps(
                provider=Provider(),
                run_id="cancel",
            ),
        ),
    )
    assert outcome.stop_reason == "stopped"
    assert not outcome.completed


def test_real_turns_record_protocol_errors_and_valid_turns():
    calls = []
    trace = SimpleNamespace(
        record_protocol_codec=lambda *a, **kw:calls.append(("codec", a, kw)),
        record_protocol_error=lambda *a, **kw:calls.append(("error", a, kw)),
        record_protocol_valid_turn=lambda *a, **kw:calls.append(("valid", a, kw)),
        record_tool_contract_hash=lambda *a, **kw:None,
        record_runtime_tool_contract_hash=lambda *a, **kw:None,
        flush=lambda:None,
    )
    prompts = []
    replies = iter(['{"tool":"invented","args":{}}', '{"tool":"done","args":{"summary":"done"}}'])
    provider = SimpleNamespace(send=lambda text:prompts.append(text) or next(replies))
    outcome = run_task_kernel(
        TaskSession(policy=TaskPolicy(grants=frozenset({"control"})), max_turns=2),
        request=KernelRunRequest(
            transport=KernelTransportDeps(
                provider=provider,
                run_id="trace",
            ),
            observation=KernelObservationDeps(
                trace_recorder=trace,
            ),
        ),
    )
    assert outcome.completed
    assert len(prompts) == 2
    assert [row[1][0] for row in calls if row[0]=="error"] == ["unknown_tool"]
    assert [row[1][0] for row in calls if row[0]=="valid"] == [2]
    assert all(row[2]["phase"]=="project" for row in calls)
