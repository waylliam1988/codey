"""Chat completion cites only exact versions from committed knowledge reads."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codey.app.headless_runner import HeadlessRequest, run_headless
from codey.knowledge.model import KnowledgeMutation, KnowledgeObject
from codey.knowledge.store import KnowledgeStore
from codey.operations.completion_context import CompletionContext
from codey.operations.kernel_facts import project_execution_record
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.research_completion_checks import knowledge_reference_checks
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall, tools_from_specs
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject
from tests.test_research_submission_handoff_production import NativeExperimentProvider, install_http, requests
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import ExperimentProvider, Target


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("read", ["none", "search", "failed", "uncommitted", "mismatched", "success"])
def test_chat_done_contract_uses_only_committed_read_versions(tmp_path: Path, native: bool, read: str) -> None:
    store = KnowledgeStore(tmp_path / "knowledge")
    try:
        store.repository.commit("seed", [KnowledgeMutation(KnowledgeObject(
            "saved-observation", "observation", "A measured observation.", "observed_result"))])
        session = TaskSession(TaskPolicy(frozenset({"control", "knowledge.read"})), task_kind="chat")
        tools = ResearchTools(None, store, None)
        delegate = ExecutionDelegate(session=session, research_tools=tools)
        before = build_turn_snapshot(session, native=native)
        if read != "none":
            call = ToolCall("knowledge_search" if read == "search" else "knowledge_read",
                            {"id": "missing" if read == "failed" else "saved-observation"}, "read-call")
            receipt = (ToolResult(call, "Search results", ok=True) if read == "search" else
                       delegate.execute(call)[0])
            session._memory_results["read-receipt"] = receipt
            if read != "uncommitted":
                project_execution_record(session, "read-receipt", call, receipt, ok=receipt.ok)
            if read == "mismatched":
                session._memory_results["read-receipt"] = ToolResult(
                    ToolCall(call.name, call.args, "another-call"), receipt.model_text, ok=True, canonical=receipt.canonical)
        snapshot = restore_snapshot(snapshot_description(build_turn_snapshot(session, native=native)))
        done = next(spec for spec in snapshot.model_specs if spec.name == "done")
        parameters = dict(done.parameters)
        assert ("knowledge_refs" in parameters) is (read == "success")
        assert "knowledge_refs" not in dict(next(spec for spec in before.model_specs if spec.name == "done").parameters)
        wire = tools_from_specs(snapshot.model_specs)
        assert ("knowledge_refs" in str(next(tool.parameters for tool in wire if tool.name == "done"))) is (read == "success")
        for identity, revision in (("saved-observation", 1), ("saved-observation", 2), ("s1", 1)):
            args: JsonObject = {"summary": "Observed result.", "knowledge_refs": [{"id": identity, "revision": revision}]}
            reply_payload: JsonObject = {"tool": "done", "args": args}
            reply = (AssistantTurn(tool_calls=(ProviderToolCall("done-call", "done", args),)) if native else
                     decode_text_turn(json.dumps(reply_payload)))
            valid = read == "success" and identity == "saved-observation" and revision == 1
            assert bool(normalize_turn(reply, snapshot=snapshot).protocol_error) is not valid
            session.last_done_args = args
            check, = knowledge_reference_checks(session, CompletionContext(knowledge_store=store))
            assert (check.status == "pass") is valid
    finally:
        store.close()


@pytest.mark.usefixtures("scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("search_first", [False, True])
def test_actual_chat_requests_and_durable_receipt_share_read_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool, search_first: bool,
) -> None:
    root = tmp_path / "knowledge"
    store = KnowledgeStore(root)
    store.repository.commit("seed", [KnowledgeMutation(KnowledgeObject(
        "saved-observation", "observation", "A measured observation.", "observed_result"))])
    store.close()
    directory = tmp_path / "run"
    directory.mkdir()
    refs: JsonObject = {"id": "saved-observation", "revision": 1}
    search: list[JsonObject] = [{"tool": "knowledge_search", "args": {"query": "measured observation"}}] if search_first else []
    install_http(monkeypatch, iter([*search,
        {"tool": "knowledge_read", "args": refs},
        {"tool": "done", "args": {"summary": "A measured observation.", "knowledge_refs": [refs]}},
    ]), native=native)
    provider_type = NativeExperimentProvider if native else ExperimentProvider
    provider = provider_type(Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10), directory,
                             fixed_acquisition=False)

    def connect(_identity: str, **_kwargs: object) -> ExperimentProvider:
        return provider

    events: list[dict[str, object]] = []
    result = run_headless(HeadlessRequest(project=None, task="Read saved-observation revision 1 and report its observation.",
        intent="chat", provider_id="local", max_turns=3, state_home=directory / "state", research_store_root=root,
        requested_capabilities=("knowledge.read",), required_tools=("knowledge_read",)),
        emit_jsonl=events.append, connect_provider=connect)
    assert result.stop_reason == "done", events
    wire = requests(directory)
    first, second = wire[0], wire[-1]
    assert "knowledge_read" in json.dumps(first["messages"])
    assert "When using saved versions, include knowledge_refs" not in json.dumps(first["messages"])
    if search_first:
        assert "knowledge_refs" not in json.dumps(wire[1].get("response_format", wire[1].get("tools")))
        assert "When using saved versions, include knowledge_refs" not in json.dumps(wire[1]["messages"])
    assert "knowledge_refs" not in json.dumps(first.get("response_format", first.get("tools")))
    assert "saved-observation" in json.dumps(second.get("response_format", second.get("tools")))
    assert "knowledge_refs" in json.dumps(second.get("response_format", second.get("tools")))
    read, = [row.result for row in tool_receipts(directory / "state") if row.result.call.name == "knowledge_read"]
    assert read.ok and read.canonical["knowledge_read_versions"] == [refs]
