"""Transport delivery cannot erase an already established completion proof."""
from types import SimpleNamespace

from codey.operations import task_loop
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.runtime.core.models import Control, ToolPlan


def test_unknown_done_delivery_retains_proof_and_separate_delivery_status(monkeypatch):
    proof = object()
    monkeypatch.setattr("codey.operations.project_verification.refresh_verification_candidates", lambda session: None)
    monkeypatch.setattr("codey.operations.completion_gate.evaluate", lambda *args, **kwargs: SimpleNamespace(complete=True, final_text="done", proof=proof))
    session = SimpleNamespace(turn=1)
    from codey.providers.api_transport import GenerationUnknownError

    class Provider:
        def acknowledge_tool_results(self, *args, **kwargs):
            raise GenerationUnknownError("receipt disconnected")

    plan = ToolPlan(calls=[], control=Control("done", "done"))
    result = task_loop._handle_done_reply(session, plan, Provider(), AssistantTurn(tool_calls=(ProviderToolCall("done1", "done", {}),)), True, [])
    assert result.proof is proof
    assert result.delivery == "unknown"
    assert result.completed is False
    assert result.stop_reason == "delivery_pending"


def test_final_delivery_and_proof_reference_survive_formal_log_restart(tmp_path):
    from codey.runtime.core.operation_state import RuntimeOperationStore
    from codey.runtime.log.session_log import RuntimeSessionLog
    from codey.runtime.write.mutation_line import RuntimeMutationLine

    log = RuntimeSessionLog(tmp_path)
    mutations = RuntimeMutationLine(log)
    mutations.accept_operation(session_id="session", run_id="run", provider_id="local", turn_budget=2, max_repair_rounds=1)
    mutations.mark_terminal("session", "run", stop_reason="delivery_pending", summary_chars=10, turns=1, max_turns=2, provider="local",
                            final_delivery="unknown", proof_ref="completion_proof:0123456789abcdef", proof_status="complete")
    recovered = RuntimeOperationStore(RuntimeSessionLog(tmp_path)).load("session", "run")
    assert recovered.final_delivery == "unknown"
    assert recovered.completion_proof_status == "complete"
    assert recovered.completion_proof_ref == "completion_proof:0123456789abcdef"
