"""Transport delivery cannot erase an already established completion proof."""

from codey.providers.base import AssistantTurn, ProviderToolCall


def test_unknown_done_delivery_retains_proof_and_separate_delivery_status(monkeypatch):
    from codey.operations import completion_gate
    from codey.operations.task_session import TaskSession
    from codey.policies.task_policy import TaskPolicy
    from codey.providers.api_transport import GenerationUnknownError
    from tests.support.kernel_runtime import KernelRunRequest, KernelTransportDeps, run_task_kernel

    proofs = []
    original = completion_gate.evaluate
    def evaluate(*args, **kwargs):
        verdict = original(*args, **kwargs)
        proofs.append(verdict.proof)
        return verdict
    monkeypatch.setattr(completion_gate, "evaluate", evaluate)

    class Provider:
        native_tools = True
        def send_turn(self, prompt, tools):
            return AssistantTurn(tool_calls=(ProviderToolCall("done1", "done", {"summary": "done"}),))
        def acknowledge_tool_results(self, *args, **kwargs):
            raise GenerationUnknownError("receipt disconnected")
        def send_tool_results(self, *args, **kwargs):
            raise AssertionError("completion must acknowledge its original call")

    result = run_task_kernel(TaskSession(TaskPolicy(frozenset({"control"}))),
                            request=KernelRunRequest(transport=KernelTransportDeps(provider=Provider())))
    assert len(proofs) == 1 and result.proof == proofs[0]
    assert result.delivery == "unknown"
    assert result.completed is False
    assert result.stop_reason == "delivery_pending"


def test_final_delivery_and_proof_reference_survive_formal_log_restart(tmp_path):
    from codey.durable import DurableSession, SQLiteDurableStore
    from codey.runtime.core.operation_state import RuntimeOperationStore
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    durable = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    durable.admit_input(conversation_id="session", request_id="request-run", task_id="run", text="test",
                        provider_snapshot={"provider_id": "local", "model_id": "test"})
    mutations = RuntimeMutationLine(durable=durable)
    mutations.accept_operation(session_id="session", run_id="run", provider_id="local", turn_budget=2, max_repair_rounds=1)
    mutations.mark_terminal("session", "run", stop_reason="delivery_pending", summary_chars=10, turns=1, max_turns=2, provider="local",
                            final_delivery="unknown", proof_ref="completion_proof:0123456789abcdef", proof_status="complete")
    durable.close()
    reopened = DurableSession(SQLiteDurableStore(tmp_path / "durable.sqlite3"))
    recovered = RuntimeOperationStore(durable=reopened).load("session", "run")
    assert recovered.final_delivery == "unknown"
    assert recovered.completion_proof_status == "complete"
    assert recovered.completion_proof_ref == "completion_proof:0123456789abcdef"
    reopened.close()
