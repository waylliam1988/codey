"""Cold completion replay preserves facts, optional claims and exact answer bytes."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from codey.durable import DurableSession, SQLiteDurableStore
from codey.durable.checkpoint import operation_from_checkpoint
from codey.operations.kernel_facts import project_execution_record
from codey.operations.task_effects import KernelEffectSink
from codey.operations.task_guidance import task_result_context, task_working_context
from codey.operations.task_loop import run_task_kernel
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import AssistantTurn, ProviderToolCall, ProviderToolResult
from codey.research.ledger import ResearchLedger
from codey.research.ledger_receipts import ledger_observation
from codey.research.tools import ResearchTools
from codey.runtime.core.execution_receipt import ExecutionReceipt, execution_receipt
from codey.runtime.core.models import ToolCall, ToolResult
from codey.runtime.write.mutation_line import RuntimeMutationLine
from codey.toolchain.tool_spec import ToolSpec
from codey.types.json_value import JsonObject
from tests.durable.test_operation_control_actions import request_for
from tests.durable.test_provider_response_receipts import runtime
from tests.support.kernel_restart import SimulatedExit

URL = "https://example.com/"
ANSWER = '{"verified":false,"exit_code":7,"reason":"页面不可读"}'


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("historical_claims", [False, True])
@pytest.mark.parametrize("retry_success", [False, True])
def test_cold_done_replays_legacy_receipts_and_optional_claims(
    tmp_path: Path, native: bool, historical_claims: bool, retry_success: bool,
) -> None:
    durable, _, outputs, sink = runtime(tmp_path, sqlite=True, turn_budget=8)
    policy = TaskPolicy(frozenset({"control", "project.verify", "web.read"}), sources_open_required=True,
                        required_checks=("research_sources_opened",))
    session = TaskSession(policy, task_kind="chat", required_source_urls=frozenset({URL}), max_turns=8)
    executions: list[str] = []
    proposal: JsonObject = {"summary": ANSWER}

    class Provider:
        native_tools = native

        def reply(self) -> ToolCall:
            if not executions:
                return ToolCall("run", {"command": "python -m pytest"}, "run-original")
            if executions == ["run"] or (retry_success and executions == ["run", "open_url"]):
                return ToolCall("open_url", {"url": URL}, "open-original")
            if historical_claims:
                run_ref = next(ref for ref, result in session._memory_results.items() if result.call.name == "run")
                open_ref = next(ref for ref, result in session._memory_results.items() if result.call.name == "open_url" and not result.ok)
                proposal.update(execution_claims=[{"result_ref": run_ref, "status": "exit",
                    "exit_code_scope": "process", "exit_code": 7}],
                    source_gaps=[{"result_ref": open_ref, "reason": "page_blank"}])
            return ToolCall("done", proposal, "done-original")

        def send(self, text: str) -> str:
            call = self.reply()
            return json.dumps({"tool": call.name, "args": call.args}, ensure_ascii=False)

        def send_turn(self, text: str, tools: list[ToolSpec]) -> AssistantTurn:
            call = self.reply()
            return AssistantTurn(tool_calls=(ProviderToolCall(call.call_id, call.name, call.args),))

        def send_tool_results(self, results: list[ProviderToolResult], tools: list[ToolSpec]) -> AssistantTurn:
            return self.send_turn("", tools)

        def acknowledge_tool_results(self, results: list[ProviderToolResult], tools: list[ToolSpec]) -> AssistantTurn:
            return AssistantTurn()

    def execute(call: ToolCall) -> ToolResult:
        executions.append(call.name)
        if call.name == "run":
            # Old audit-only format intentionally has no canonical execution.
            return ToolResult(call, "legacy process output", ok=False, audit={"exit_code": 7})
        if executions.count("open_url") == 2:
            ledger = ResearchLedger()
            ledger.record_open(URL, URL, "Readable", "observed body")
            return ToolResult(call, "observed body", ok=True,
                              canonical={"research_observation": ledger_observation(ledger, "open_url", url=URL)})
        return ToolResult(call, "ERROR: page_blank", ok=False,
                          canonical={"source_failure": {"url": URL, "detail": "page_blank"}})

    armed = True

    def pause(*_args: object) -> None:
        nonlocal armed
        state = operation_from_checkpoint(durable.task("r"), "s")
        if armed and state is not None and state.leaf == "completion_pending":
            armed = False
            raise SimulatedExit()

    durable.subscribe_commits(pause)
    request = request_for(durable, sink, Provider(), executors={"run": execute, "open_url": execute})
    request = replace(request, execution=replace(request.execution, research_tools=ResearchTools(None, None, None)))
    try:
        with pytest.raises(SimulatedExit):
            run_task_kernel(session, request=request)
    finally:
        durable.unclaim_task("r")
        durable.close()
    expected_executions = ["run", "open_url"] + (["open_url"] if retry_success else [])
    assert executions == expected_executions

    class NoRequests(Provider):
        def send(self, text: str) -> str:
            raise AssertionError(f"saved done must not ask the model again: {text[-3000:]}")

        def send_tool_results(self, results: list[ProviderToolResult], tools: list[ToolSpec]) -> AssistantTurn:
            raise AssertionError(f"saved done must not ask the model again: {results}")

    reopened = DurableSession(SQLiteDurableStore(tmp_path / "runtime.sqlite"))
    reopened.recover_scheduler_state()
    reopened.claim_task("r")
    mutations = RuntimeMutationLine(durable=reopened)
    mutations.mark_writer_running("s", "r", provider_id="local")
    recovered_sink = KernelEffectSink(mutations, session_id="s", run_id="r", provider_id="local",
                                      durable=reopened, managed_outputs=outputs)

    def forbidden(call: ToolCall) -> ToolResult:
        raise AssertionError(f"recovery repeated {call.name}")

    fresh = replace(session, executed={}, _memory_results={}, restored_effect_ids=set(), last_done_args={}, last_done_text="",
                    opened_sources=set(), source_ids={})
    resumed = request_for(reopened, recovered_sink, NoRequests(), executors={"run": forbidden, "open_url": forbidden})
    resumed = replace(resumed, execution=replace(resumed.execution, research_tools=ResearchTools(None, None, None)))
    try:
        outcome = run_task_kernel(fresh, request=resumed)
        assert outcome.stop_reason == ("done" if retry_success else "blocked") and outcome.summary == ANSWER
        assert fresh.last_done_args == proposal
        assert len(fresh._memory_results) == len(expected_executions)
        result = next(result for result in fresh._memory_results.values() if result.call.name == "run")
        receipt = execution_receipt(result)
        assert receipt is not None and receipt.exit_code == 7 and receipt.exit_code_scope == "process"
        assert bool(fresh.opened_sources) is retry_success
        assert executions == expected_executions
    finally:
        reopened.close()


@pytest.mark.parametrize("name,scope,status,code", [
    ("run", "process", "exit", 7), ("shell", "shell", "exit", 1),
    ("shell", "shell", "timeout", None),
])
def test_context_queries_are_pure_and_use_receipt_state(
    name: str, scope: str, status: str, code: int | None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codey.operations import kernel_execution
    from codey.operations.tool_result_reader import read_tool_result
    from codey.research.chat_web import ChatWebProvider
    from codey.research.source_document import SourceDocument
    from codey.research.source_gateway import ResearchSourceGateway
    from tests.test_execution_claims_and_output_recovery import Reader

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("rendering must not execute or fetch")

    monkeypatch.setattr(kernel_execution, "execute_turn", forbidden)
    monkeypatch.setattr(ChatWebProvider, "fetch", forbidden)
    monkeypatch.setattr(ResearchSourceGateway, "open", forbidden)
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read"})), task_kind="chat",
                          required_source_urls=frozenset({URL}))
    receipt = ExecutionReceipt(status, code, "output", exit_code_scope=scope)
    result = ToolResult(ToolCall(name, {"command": "test command"}), receipt.render(), ok=True,
                        canonical={"execution": receipt.facts()})
    project_execution_record(session, "exec", result.call, result, ok=True, exit_code=code)
    session._memory_results["exec"] = result
    session.record_open(URL)
    ledger = ResearchLedger()
    ledger.record_open_document(SourceDocument(URL, URL, "Readable", "html", "text/html", "observed body"))
    before_session, before_ledger = session.clone_projection(), ledger.clone()
    rendered = task_working_context(session, ledger)
    facts, _ = json.JSONDecoder().raw_decode(rendered.split("Current work observations (runtime records):\n")[1])
    observed = facts["executions"][0]
    assert observed["status"] == status and observed["exit_code"] == code
    assert observed["execution"]["exit_code_scope"] == scope
    assert "Execution facts: " in task_result_context(session, result, None)
    recovered_output = read_tool_result(Reader(session, None), ToolCall("read_tool_result", {"result_ref": "exec"}))
    assert recovered_output.ok and "output" in recovered_output.model_text
    assert task_working_context(session, ledger) == rendered
    assert session == before_session and session._memory_results == before_session._memory_results
    assert vars(ledger) == vars(before_ledger)


def test_chat_source_view_uses_ledger_aliases_without_duplicate_session_summary() -> None:
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read"})), task_kind="chat")
    ledger = ResearchLedger()
    final = "https://example.com/final"
    ledger.record_open(URL, final, "Redirected source", "Observed page body")
    before = ledger.clone()
    rendered = task_working_context(session, ledger)
    marker = "Source coverage (verified runtime observations):\n"
    facts, _ = json.JSONDecoder().raw_decode(rendered.split(marker)[1])
    assert facts["opened_urls"] == sorted([URL, final])
    assert facts["required_urls"] == []
    assert rendered.count(marker) == 1 and "Opened external sources" not in rendered
    assert not session.opened_sources and vars(ledger) == vars(before)
