"""Frozen local contracts give consistent instructions on both wire protocols."""

import copy
import json

import pytest

from codey.operations import kernel_prompt
from codey.operations.kernel_preparation import prepare_kernel_turn
from codey.operations.kernel_protocol import build_turn_snapshot
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_guidance import task_guidance_for_policy
from codey.policies.task_policy import TaskPolicy
from codey.research.methods import research_method_guidance
from codey.runtime.core.models import ToolCall, ToolPlan
from tests.test_research_findings import URL, execute
from tests.test_research_local_scope import local_setup, run_local


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
def test_local_preparation_and_restored_repairs_follow_only_frozen_completion(tmp_path, native):
    store, tools, session, delegate = local_setup(tmp_path)
    session.policy = TaskPolicy(session.policy.grants, strict_research=True)
    try:
        prepared = prepare_kernel_turn(session, user_task=session.task_text, context_text="",
            task_guidance=task_guidance_for_policy(session.policy), native=native)
        assert prepared.snapshot.research_scope is not None
        assert "submit_research_answer" in prepared.prompt
        assert "report_sections.coverage" not in prepared.prompt
        assert "final answer with done" not in prepared.prompt
        assert "Shell runs" not in prepared.prompt and "CodeMode" not in prepared.prompt
        assert "clinical" not in prepared.prompt
        restored = restore_snapshot(snapshot_description(prepared.snapshot))
        repair = kernel_prompt._repair_prompt(ToolPlan(calls=[], control=None, protocol_error="bad call"),
                                              snapshot=restored, native=native)
        assert "Call done" not in repair and "submit_research_answer" in repair
        followup = kernel_prompt._completion_followup("Repair this target", native=native,
            structured_report=True, local_submission=True)
        assert "Call done" not in followup
        if native:
            assert "submit_research_answer" in followup
        # Saving is factual data and does not introduce another completion contract.
        result = execute(delegate, tools, session, "open_url", {"url": URL})
        session._memory_results["read"] = result
        result = execute(delegate, tools, session, "save_finding", {
            "claim": "Only one writer can write at a time.", "passage_ref": next(iter(tools.ledger.read_passages))})
        data = json.loads(result.model_text)
        assert data["interpretation_review"] == "unreviewed" and data["finding_ref"] == "f1"
        assert "next" not in data
        snapshot = build_turn_snapshot(session, native=native)
        assert "done" not in snapshot.tool_names
        assert "done adopts" not in snapshot.contract_text
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
def test_actual_local_payloads_do_not_reintroduce_global_save_instructions(tmp_path, monkeypatch, native):
    from codey.providers import api_transport
    from codey.providers.api_provider import ApiProvider

    sent = []
    providers = []

    def wrap(scripted):
        def generate(_url, payload, *_args, **_kwargs):
            sent.append(copy.deepcopy(payload))
            call = json.loads(scripted.send("\n".join(str(row.get("content", "")) for row in payload["messages"])))
            message = ({"tool_calls": [{"id": f"call-{len(sent)}", "type": "function", "function": {
                "name": call["tool"], "arguments": json.dumps(call["args"])}}]} if native else
                {"content": json.dumps(call)})
            return {"choices": [{"message": message, "finish_reason": "stop"}]}

        monkeypatch.setattr(api_transport, "generate", generate)
        actual = ApiProvider("http://fixture/v1", "fixture", native_tools=native, stream=False)
        providers.append(actual)
        return actual

    try:
        result = run_local(tmp_path, native=native, provider_wrap=wrap)
        assert result.stop_reason == "done", result.summary
        assert len(result.accepted_report.selection_receipts) == 3
        assert len(sent) == 9
        assert all("in done.report_sections.coverage[].findings" not in json.dumps(payload) for payload in sent)
    finally:
        for provider in providers:
            provider.close()


def test_domain_transfer_is_not_a_clinical_signal():
    assert "clinical:" not in research_method_guidance("如何继续转移数据？", finding_report=True)
    assert "clinical:" in research_method_guidance("癌症转移患者的治疗研究", finding_report=True)


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
def test_recall_guidance_follows_frozen_scope_without_changing_ordinary_or_source_text(tmp_path, native):
    store, tools, session, ordinary = local_setup(tmp_path)
    try:
        execute(ordinary, tools, session, "open_url", {"url": URL})
        saved = execute(ordinary, tools, session, "save_finding", {
            "claim": "Only one writer can write at a time. The source mentions done.",
            "passage_ref": next(iter(tools.ledger.read_passages))})
        assert saved.ok
        session.policy = TaskPolicy(session.policy.grants, strict_research=True)
        snapshot = restore_snapshot(snapshot_description(build_turn_snapshot(session, native=native)))
        local = ExecutionDelegate(session=session, research_tools=tools, research_scope=snapshot.research_scope)
        search = ToolCall("knowledge_search", {"query": "writer", "question_ref": "q1"})
        recalled, ok, _ = local._execute_research(search)
        assert ok and "Previously saved knowledge" in recalled.model_text
        assert "in done " not in recalled.model_text
        assert "source mentions done" in recalled.model_text  # Quoted data is untouched.
        normal, ok, _ = ordinary._execute_research(search)
        assert ok and "in done " in normal.model_text
        note_id = saved.canonical["knowledge_notes"]["created"][0]
        # Old document notes must not introduce a second completion contract.
        tools.created_ids.clear()
        read, ok, _ = local._execute_research(ToolCall("knowledge_read", {"id": note_id, "question_ref": "q1"}))
        assert ok and "done.knowledge_refs" not in read.model_text
        assert "submit_research_answer" in read.model_text
        assert not tools.local_submission  # The per-call facade does not leak.
    finally:
        store.close()

