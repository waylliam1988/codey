"""Inline declarations reuse the existing write effect, receipts and publication."""

import copy
import hashlib
import json
import os
import subprocess
import sys
from contextlib import nullcontext
from dataclasses import replace
from unittest.mock import patch

import pytest

from codey.operations.kernel_facts import project_result_observation
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_execution import ExecutionDelegate
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.providers import api_generation
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.research.local_scope import ResearchScope, current_scope, question_details, submission
from codey.research.passages import passage_ref
from codey.runtime.core.models import ToolCall
from tests.test_research_deterministic_completion import _run
from tests.test_research_findings import URL, execute, setup

TASK = "Explain writer concurrency."
TEXT = "Readers and a writer may run concurrently. Only one writer at a time."
CLAIM = "Only one writer can write at a time."


def inline_setup(root, *, mode="inline"):
    store, tools, session, delegate = setup(root)
    session.policy = TaskPolicy(session.policy.grants, strict_research=True)
    session.task_kind = "research"
    result, ok, _ = delegate._execute_research(ToolCall("research_plan", {
        "questions": ["What limits writer concurrency?"],
        "question_details": [{"task_refs": ["r1"], "submission_mode": mode}]}))
    assert ok, result.model_text
    project_result_observation(session, result.call, result, ok=True, research_ledger=tools.ledger)
    session._memory_results["plan"] = result
    session._memory_results["read"] = execute(delegate, tools, session, "open_url", {"url": URL})
    scope = current_scope(session)
    return store, tools, session, scope


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("state", ["answered", "partial"])
def test_inline_exact_write_and_choice_share_one_receipt_and_retry_identity(tmp_path, native, state):
    store, tools, session, scope = inline_setup(tmp_path)
    try:
        snapshot = build_turn_snapshot(session, native=native)
        restored = restore_snapshot(snapshot_description(snapshot))
        assert restored.research_scope == scope and scope.schema_version == 3
        assert "save_finding" not in restored.tool_names
        spec = next(row for row in restored.frozen_specs if row.name == "submit_research_answer")
        inner = dict(spec.parameters)["finding"]
        assert "question_ref" not in inner["properties"]
        for example in spec.json_examples:
            assert not normalize_turn(decode_text_turn(example), snapshot=restored).protocol_error
        args = {"state": state, "finding": {"claim": CLAIM, "passage_ref": scope.passage_refs[0]}}
        reply = (AssistantTurn(tool_calls=(ProviderToolCall("c1", spec.name, args),)) if native else
                 decode_text_turn(json.dumps({"tool": spec.name, "args": args})))
        plan = normalize_turn(reply, snapshot=restored)
        assert not plan.protocol_error, plan.protocol_error
        delegate = ExecutionDelegate(session=session, research_tools=tools, research_scope=scope)
        result, ok, _ = delegate._execute_research(plan.calls[0])
        assert ok, result.model_text
        assert result.call.name == "submit_research_answer"
        assert result.canonical["research_answer"]["state"] == state
        assert result.canonical["research_answer"]["findings"][0]["ref"] == "f1"
        assert len(result.canonical["knowledge_notes"]["created"]) == 1
        assert "claim" not in result.canonical["research_answer"]
        assert json.loads(result.model_text)["interpretation_review"] == "unreviewed"
        assert scope.finding_refs == ()  # No mutation/bypass of the frozen candidate set.
        project_result_observation(session, result.call, result, ok=True, research_ledger=tools.ledger)
        assert session.notes_saved == 1
        # Retry after a write but before selection settlement reuses its exact identity.
        repeated, ok, _ = delegate._execute_research(plan.calls[0])
        assert ok, repeated.model_text
        assert repeated.canonical["research_answer"] == result.canonical["research_answer"]
        assert len(tools.created_ids) == 1
        session._memory_results["submit"] = result
        assert current_scope(session) is None
        stale, ok, _ = delegate._execute_research(plan.calls[0])
        assert not ok and "stale" in stale.model_text
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("mutation", ["both", "unresolved", "missing_state", "wrong_question", "redundant_question", "unknown_passage",
    "duplicate_passage", "wrong_type", "invalid_conditions", "unknown_finding", "empty_answer"])
def test_bad_inline_choices_never_write_or_select(tmp_path, mutation):
    store, tools, session, scope = inline_setup(tmp_path)
    try:
        args = {"state": "answered", "finding": {"claim": CLAIM, "passage_ref": scope.passage_refs[0]}}
        if mutation == "both":
            args["findings"] = []
        elif mutation == "unresolved":
            args["state"] = "unresolved"
        elif mutation == "missing_state":
            del args["state"]
        elif mutation == "wrong_question":
            args["finding"]["question_ref"] = "q2"
        elif mutation == "redundant_question":
            args["finding"]["question_ref"] = scope.question_ref
        elif mutation == "unknown_passage":
            args["finding"]["passage_ref"] = "p_unknown"
        elif mutation == "duplicate_passage":
            args["finding"]["additional_passage_refs"] = [scope.passage_refs[0]]
        elif mutation == "wrong_type":
            args["finding"]["claim"] = 42
        elif mutation == "invalid_conditions":
            args["finding"]["conditions"] = [{"name": "scope", "state": "invented"}]
        elif mutation == "unknown_finding":
            args = {"state": "answered", "findings": ["f99"]}
        else:
            args = {"state": "answered", "findings": []}
        result, ok, _ = ExecutionDelegate(session=session, research_tools=tools, research_scope=scope)._execute_research(
            ToolCall("submit_research_answer", args))
        assert not ok, result.model_text
        assert not tools.created_ids and not tools.updated_ids
        assert "research_answer" not in result.canonical and current_scope(session) == scope
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_gap_writes_nothing_and_old_scope_still_rejects_inline(tmp_path):
    store, tools, session, scope = inline_setup(tmp_path)
    try:
        delegate = ExecutionDelegate(session=session, research_tools=tools, research_scope=scope)
        result, ok, _ = delegate._execute_research(ToolCall("submit_research_answer", {"state": "unresolved", "findings": []}))
        assert ok and result.canonical["research_answer"]["findings"] == []
        assert "knowledge_notes" not in result.canonical and not tools.created_ids
        old = replace(scope, schema_version=2, submission_mode="select")
        assert "submission_mode" not in old.to_payload()
        assert ResearchScope.from_payload(old.to_payload()) == old
        with pytest.raises(ValueError, match="existing findings"):
            submission(session, tools.ledger, old, {"state": "answered", "finding": {
                "claim": CLAIM, "passage_ref": scope.passage_refs[0]}})
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_inline_mode_can_select_existing_finding_but_never_silently_refresh_its_version(tmp_path):
    store, tools, session, scope = inline_setup(tmp_path)
    try:
        saved = execute(ExecutionDelegate(session=session, research_tools=tools), tools, session, "save_finding", {
            "claim": CLAIM, "passage_ref": scope.passage_refs[0]})
        session._memory_results["saved"] = saved
        scope = current_scope(session)
        assert scope.finding_refs == ("f1",)
        args = {"state": "answered", "findings": ["f1"]}
        delegate = ExecutionDelegate(session=session, research_tools=tools, research_scope=scope)
        result, ok, _ = delegate._execute_research(ToolCall("submit_research_answer", args))
        assert ok, result.model_text
        assert "knowledge_notes" not in result.canonical and len(tools.created_ids) == 1
        original = tools.ledger.references["f1"]
        tools.ledger.references["f1"] = replace(original, revision=original.revision + 1)
        failed, ok, _ = delegate._execute_research(ToolCall("submit_research_answer", args))
        assert not ok and "frozen request" in failed.model_text
    finally:
        store.close()


@pytest.mark.parametrize("args", [
    {"state": "unresolved", "findings": ["f1"]}, {"state": "answered", "findings": []},
    {"state": "answered", "findings": ["f1", "f1"]}, {"state": "answered", "findings": "f1"},
    {"state": "unresolved", "finding": {}}, {"state": True, "findings": []},
    {"state": "answered", "finding": {}, "findings": []},
])
def test_replay_rejects_changed_or_ambiguous_selection_shapes(args):
    from codey.runtime.effects.safe_tool_replay import replay_args_for_tool_call

    assert replay_args_for_tool_call(ToolCall("submit_research_answer", args)) is None


@pytest.mark.parametrize("mode", [True, [], "automatic"])
def test_plan_mode_is_explicit_validated_and_old_scope_codecs_do_not_gain_fields(mode):
    with pytest.raises(ValueError, match="submission_mode"):
        question_details([{"task_refs": ["r1"], "submission_mode": mode}], ("question",), TASK)
    old = ResearchScope(1, "a" * 64, "q1", "q1.u1", "parent", "target", (), (), ())
    assert ResearchScope.from_payload(old.to_payload()) == old
    with pytest.raises(ValueError):
        ResearchScope.from_payload({**old.to_payload(), "submission_mode": "inline"})


def run_inline(root, *, native=False, resume=False, boundary="", all_gap=False, state="answered", repair=False,
               provider_wrap=None):
    def factory(provider):
        original = provider.send

        def send(prompt):
            previous = json.loads(original(prompt))
            calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
            turn = next(row["turn"] for row in reversed(calls) if "turn" in row)
            if turn == 1:
                previous["args"] = {"questions": ["What limits writer concurrency?"],
                    "question_details": [{"task_refs": ["r1"], "submission_mode": "inline"}]}
            elif turn >= 4:
                assert turn == 4 or repair and turn == 5, prompt[-1500:]
                text = CLAIM if not repair else ("同一时刻只有一个写者。" * 6 if turn == 4 else
                                                 "WAL模式下，同一时刻只有一个写者，写者串行。")
                args = ({"state": "unresolved", "findings": []} if all_gap else {"state": state, "finding": {
                    "claim": text, "passage_ref": passage_ref(URL, hashlib.sha256(TEXT.encode()).hexdigest()[:16], 0, len(TEXT))}})
                previous = {"tool": "submit_research_answer", "args": args}
            return json.dumps(previous)

        provider.send = send
        return provider_wrap(provider) if provider_wrap else provider

    def after_write(original, *args, **kwargs):
        value = original(*args, **kwargs)
        if value[1]:
            os._exit(74)
        return value

    original_write = ExecutionDelegate._execute_research_write
    interruption = (patch.object(ExecutionDelegate, "_execute_research_write",
                    lambda *args, **kwargs: after_write(original_write, *args, **kwargs))
                    if boundary == "inline_written_before_receipt" else nullcontext())
    with interruption:
        return _run(root, native=native, resume=resume, boundary=boundary, boundary_turn=4, budget=8,
            task=TASK if not repair else "Explain writer concurrency. 给出约20字的中文结论。", provider_factory=factory)


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("kind", ["answered", "partial", "gap", "length_repair"])
def test_inline_production_flow_uses_original_gate_compilation_and_publication(tmp_path, native, kind):
    result = run_inline(tmp_path, native=native, all_gap=kind == "gap", repair=kind == "length_repair",
                        state="partial" if kind == "partial" else "answered")
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    if kind == "gap":
        assert result.stop_reason == "research_unresolved" and result.accepted_report is None
        assert not any("publication" in row for row in calls)
    else:
        assert result.stop_reason == "done", result.summary
        report = result.accepted_report
        assert len(report.selection_receipts) == 1 and len(report.finding_ids) == 1
        assert report.coverage[0]["state"] == ("partial" if kind == "partial" else "evidence_found")
        assert report.coverage[0]["findings"] == (["f2"] if kind == "length_repair" else ["f1"])
        assert all(row["interpretation_review"] == "unreviewed" for row in report.reference_metadata.values())
        assert sum("publication" in row for row in calls) == 1
    assert result.turns == (5 if kind == "length_repair" else 4)
    before = (tmp_path / "calls.jsonl").read_bytes()
    assert run_inline(tmp_path, native=native, resume=True, all_gap=kind == "gap", repair=kind == "length_repair",
                      state="partial" if kind == "partial" else "answered") == result
    assert (tmp_path / "calls.jsonl").read_bytes() == before


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("boundary,native", [*((boundary, False) for boundary in (
    "provider_request_ready", "provider_response_ready", "tool_effect_pending", "inline_written_before_receipt",
    "tool_delivery_pending", "kernel_settled", "control_ready", "publication_admitted", "publication_committed", "publication_receipt")),
    ("provider_response_ready", True), ("inline_written_before_receipt", True)])
def test_inline_real_process_restart_never_duplicates_claim_selection_or_publication(tmp_path, monkeypatch, boundary, native):
    code = ("from pathlib import Path; from tests.test_research_inline_submission import run_inline; import sys; "
            "run_inline(Path(sys.argv[1]), boundary=sys.argv[2], native=sys.argv[3]=='True')")
    process = subprocess.run([sys.executable, "-c", code, str(tmp_path), boundary, str(native)], capture_output=True, timeout=70)
    assert process.returncode == 74, process.stderr.decode(errors="replace")
    if boundary in {"kernel_settled", "control_ready", "publication_admitted", "publication_committed", "publication_receipt"}:
        monkeypatch.setattr("codey.operations.research_completion_checks.compile_finding_report",
                            lambda *a, **k: pytest.fail("accepted report was recompiled"))
    result = run_inline(tmp_path, resume=True, native=native)
    assert result.stop_reason == "done", result.summary
    assert len(result.accepted_report.selection_receipts) == len(result.accepted_report.finding_ids) == 1
    rows = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert sum("attempt" in row for row in rows) == 4
    assert sum("publication" in row for row in rows) == sum("fetch" in row for row in rows) == 1
    from codey.knowledge.store import KnowledgeStore

    store = KnowledgeStore(tmp_path / "knowledge")
    try:
        assert store.index._read_conn().execute("SELECT COUNT(*) FROM knowledge_versions WHERE kind='claim'").fetchone()[0] == 1
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
def test_actual_inline_requests_share_frozen_schema_and_hide_the_old_save_action(tmp_path, monkeypatch, native):
    from codey.providers.api_provider import ApiProvider

    sent, providers = [], []

    def wrap(scripted):
        def generate(_url, payload, *_args, **_kwargs):
            sent.append(copy.deepcopy(payload))
            raw = scripted.send("\n".join(str(row.get("content", "")) for row in payload["messages"]))
            call = json.loads(raw)
            message = ({"tool_calls": [{"id": f"c{len(sent)}", "type": "function", "function": {
                "name": call["tool"], "arguments": json.dumps(call["args"])}}]} if native else {"content": raw})
            return {"choices": [{"message": message, "finish_reason": "stop"}]}

        monkeypatch.setattr(api_generation, "generate", generate)
        provider = ApiProvider("http://fixture/v1", "fixture", native_tools=native, stream=False,
                               thinking_enabled=False, text_tool_format="plain" if native else "json_schema")
        providers.append(provider)
        return provider

    try:
        result = run_inline(tmp_path, native=native, provider_wrap=wrap)
        assert result.stop_reason == "done" and len(sent) == 4
        local_request = sent[-1]
        serialized = json.dumps(local_request)
        assert '"name": "save_finding"' not in serialized
        assert "Otherwise save the supported local answer once" not in serialized
        assert "report_sections.coverage" not in serialized
        assert "finding" in serialized and "unresolved" in serialized
        if native:
            schema = next(tool["function"]["parameters"] for tool in local_request["tools"]
                          if tool["function"]["name"] == "submit_research_answer")
            assert schema["required"] == ["state"]
            assert schema["properties"]["finding"]["additionalProperties"] is False
            assert schema["properties"]["finding"]["properties"]["passage_ref"]["enum"]
        else:
            root = local_request["response_format"]["json_schema"]["schema"]
            variants = root.get("oneOf", [root])
            variant = next(row for row in variants
                           if row["allOf"][0]["properties"]["tool"]["enum"] == ["submit_research_answer"])
            schema = variant["allOf"][1]["properties"]["args"]
        assert "question_ref" not in schema["properties"]["finding"]["properties"]
    finally:
        for provider in providers:
            provider.close()
