"""Compact entry, lossless origins, original report gate and frozen replay."""

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
from codey.operations.kernel_preparation import prepare_kernel_turn
from codey.operations.kernel_protocol import normalize_turn
from codey.operations.provider_receipts import restore_snapshot, snapshot_description
from codey.operations.task_execution import ExecutionDelegate
from codey.operations.task_guidance import task_guidance_for_policy, task_working_context
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.research.local_scope import current_scope, plan_units, registered_plan
from codey.research.plan_contract import compact_plan, task_layout, validate_compact_plan
from codey.research.task_requirements import coverage_requirements, validate_task_accounting
from codey.runtime.core.models import ToolCall
from tests.manual.research_framework_comparison import SYNTHESIS
from tests.test_research_deterministic_completion import _run
from tests.test_research_findings import URL, setup

TASK = "请根据材料研究：\n1. What limits writer concurrency?\n2. What local measurements exist?\n用中文作答。不得猜测。"


@pytest.mark.parametrize("task", [TASK, *(row["question"] for row in SYNTHESIS.values())])
def test_complete_numbered_questions_keep_every_byte_and_application_owns_accounting(task):
    plan = compact_plan(None, task)
    expected = 2 if task == TASK else 5
    assert len(plan_units(plan)) == expected
    assert len(coverage_requirements(task)) > expected
    spans = sorted([*plan["task_layout"]["task_questions"], *plan["task_layout"]["other_task_context"]], key=lambda row: row["start"])
    assert "".join(row["text"] for row in spans) == task
    assert all(row["text"] == task[row["start"]:row["end"]] for row in spans)
    rows = [{"task_refs": row["task_refs"]} for row in plan_units(plan)]
    validate_task_accounting(rows, task)  # The original final accounting gate.
    assert validate_compact_plan(plan, task) == tuple(row["text"] for row in plan["questions"])


def test_unstructured_task_and_unassigned_continuations_are_not_silently_discarded():
    task = "Compare A OR B, but not C. Retain exceptions; do not infer performance."
    assert task_layout(task)["task_questions"][0]["text"] == task
    multiline = TASK.replace("2. What", "  Keep A OR B; except C.\n2. What")
    layout = task_layout(multiline)
    assert "Keep A OR B; except C." in layout["task_questions"][0]["text"]
    assert any("不得猜测" in row["text"] for row in layout["other_task_context"])
    plan = compact_plan([{"text": "Compare both relations, retaining exceptions.", "task_refs": ["t1", "t2"]}], multiline)
    validate_task_accounting([{"task_refs": plan["questions"][0]["task_refs"]}], multiline)
    with pytest.raises(ValueError, match="missing"):
        compact_plan([{"text": "Only one relation", "task_refs": ["t1"]}], TASK)
    corrupt = copy.deepcopy(plan)
    corrupt["questions"][0]["task_refs"] = []
    with pytest.raises(ValueError, match="original task"):
        validate_compact_plan(corrupt, multiline)


@pytest.mark.parametrize("separator", ["\n", "\r\n", "\n\n"])
def test_numbering_and_whitespace_do_not_assign_other_questions(separator):
    task = "1. 为什么 A？" + separator + "2. 为什么 B？"
    plan = compact_plan(None, task)
    assert [row["task_refs"] for row in plan["questions"]] == [["r1", "r2"], ["r3", "r4"]]
    legacy = compact_plan(None, task, schema_version=2)
    assert all(row["task_refs"] == ["r1", "r2", "r3", "r4"] for row in legacy["questions"])
    assert validate_compact_plan(legacy, task) == validate_compact_plan(plan, task)
    legacy["schema_version"] = 3
    with pytest.raises(ValueError, match="original task spans"):
        validate_compact_plan(legacy, task)


def test_global_constraints_bind_both_questions_without_cross_assigning_question_text():
    task = "全局限制：不能猜测。\n1. 为什么 A？\n2. 为什么 B？\n保留缺口。"
    plan = compact_plan(None, task)
    assert [row["task_refs"] for row in plan["questions"]] == [
        ["r1", "r2", "r3", "r6"], ["r1", "r4", "r5", "r6"]]
    validate_task_accounting(plan["questions"], task)


@pytest.mark.parametrize("native", [False, True])
def test_new_entry_one_object_array_and_old_frozen_plan_remains_legacy(tmp_path, native):
    store, tools, session, _ = setup(tmp_path)
    session.task_text = TASK
    session.research_questions = tools.ledger.questions = ()
    session.policy = TaskPolicy(session.policy.grants, strict_research=True)
    try:
        prepared = prepare_kernel_turn(session, user_task=TASK, context_text="",
            task_guidance=task_guidance_for_policy(session.policy), native=native)
        snapshot = restore_snapshot(snapshot_description(prepared.snapshot))
        spec = next(row for row in snapshot.model_specs if row.name == "research_plan")
        assert "question_details" not in dict(spec.parameters)
        assert "done" not in snapshot.tool_names
        assert "report_sections.coverage" not in prepared.prompt
        assert "coverage_task_refs" not in task_working_context(session, tools.ledger, store)
        reply = normalize_turn(decode_text_turn('{"tool":"research_plan","args":{}}'), snapshot=snapshot)
        assert not reply.protocol_error, reply.protocol_error
        delegate = ExecutionDelegate(session=session, research_tools=tools,
                                     frozen_specs={row.name: row for row in snapshot.frozen_specs})
        result, ok, _ = delegate._execute_research(reply.calls[0])
        assert ok, result.model_text
        project_result_observation(session, result.call, result, ok=ok, research_ledger=tools.ledger)
        session._memory_results["plan"] = result
        assert len(session.research_questions) == 2
        assert current_scope(session).target == "What limits writer concurrency?"
        assert current_scope(session).submission_mode == "inline"
        context = task_working_context(session, tools.ledger, store)
        assert '"task_ref":"t1"' in context and "不得猜测" in context
        assert registered_plan(session)["schema_version"] == 3
        # Direct legacy facade calls and old receipts keep their input contract.
        old_store, old_tools, old_session, old_delegate = setup(tmp_path / "old")
        try:
            old, ok, _ = old_delegate._execute_research(ToolCall("research_plan", {
                "questions": ["What limits writer concurrency?"], "question_details": [{"task_refs": ["r1"]}]}))
            assert ok and "schema_version" not in old.canonical["research_observation"]
        finally:
            old_store.close()
    finally:
        store.close()


def run_compact(root, *, native=False, resume=False, boundary="", task=TASK, reuse_answer=False):
    def factory(provider):
        original = provider.send

        def send(prompt):
            original(prompt)  # Logs the real kernel's logical turn.
            turns = [json.loads(line)["turn"] for line in (root / "calls.jsonl").read_text().splitlines()
                     if "turn" in json.loads(line)]
            turn = turns[-1]
            if turn == 1:
                row = {"tool": "research_plan", "args": {}}
            elif turn == 2:
                row = {"tool": "open_url", "args": {"question_ref": "q1", "url": URL}}
            elif turn == 3 or turn == 4 and reuse_answer:
                from codey.research.passages import passage_ref

                text = "Readers and a writer may run concurrently. Only one writer at a time."
                row = {"tool": "submit_research_answer", "args": {"state": "answered", "finding": {
                    "claim": ("根据当前材料，同一时刻只有一个写者。" if turn == 3 else
                              "根据当前材料，读者与一个写者可以并发运行。"),
                    "passage_ref": passage_ref(URL, hashlib.sha256(text.encode()).hexdigest()[:16], 0, len(text))}}}
            elif turn == 4:
                row = {"tool": "submit_research_answer", "args": {"state": "unresolved", "findings": []}}
            else:
                pytest.fail(f"unexpected generation {turn}: {prompt[-1000:]}")
            return json.dumps(row)

        provider.send = send
        return provider

    original_write = ExecutionDelegate._execute_research_write

    def interrupted_write(*args, **kwargs):
        value = original_write(*args, **kwargs)
        if value[1]:
            os._exit(74)
        return value

    interruption = (patch.object(ExecutionDelegate, "_execute_research_write", interrupted_write)
                    if boundary == "inline_written_before_receipt" else nullcontext())
    with interruption:
        return _run(root, native=native, resume=resume, boundary=boundary, task=task, compact_planning=True,
                    provider_factory=factory, boundary_turn=3 if boundary in {
                        "provider_request_ready", "provider_response_ready", "tool_effect_pending"} else 4)


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("native", [False, True])
def test_compact_plan_answer_and_gap_pass_original_report_gate_and_replay(tmp_path, native):
    result = run_compact(tmp_path, native=native)
    assert result.stop_reason == "done", result.summary
    assert len(result.accepted_report.coverage) == 2
    assert result.accepted_report.coverage[1]["findings"] == []
    validate_task_accounting(list(result.accepted_report.coverage), TASK)
    before = (tmp_path / "calls.jsonl").read_bytes()
    assert run_compact(tmp_path, native=native, resume=True) == result
    assert (tmp_path / "calls.jsonl").read_bytes() == before


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("boundary", ["provider_request_ready", "provider_response_ready", "tool_effect_pending",
    "inline_written_before_receipt", "tool_delivery_pending", "kernel_settled", "control_ready",
    "publication_admitted", "publication_committed", "publication_receipt"])
def test_compact_plan_survives_cold_restart_without_duplicate_write_or_publication(tmp_path, boundary):
    source = ("from pathlib import Path; from tests.test_research_compact_plan import run_compact; import sys; "
              "run_compact(Path(sys.argv[1]), boundary=sys.argv[2])")
    child = subprocess.run([sys.executable, "-c", source, str(tmp_path), boundary], capture_output=True, timeout=50)
    assert child.returncode == 74, child.stderr.decode(errors="replace")
    result = run_compact(tmp_path, resume=True)
    assert result.stop_reason == "done", result.summary
    rows = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert sum("publication" in row for row in rows) == sum("fetch" in row for row in rows) == 1


@pytest.mark.parametrize("version, mode", [(2, "select"), (3, "inline")])
def test_old_compact_frozen_entry_keeps_its_accounting_and_submission_mode(tmp_path, version, mode):
    from codey.operations.kernel_protocol import build_turn_snapshot

    store, tools, session, _ = setup(tmp_path)
    session.task_text = TASK
    session.research_questions = tools.ledger.questions = ()
    session.policy = TaskPolicy(session.policy.grants, strict_research=True)
    try:
        new = build_turn_snapshot(session)
        invalid = normalize_turn(decode_text_turn(json.dumps({"tool": "research_plan", "args": {"contract_version": version}})), snapshot=new)
        assert invalid.protocol_error
        specs = tuple(replace(spec, parameters=tuple({**dict(spec.parameters),
            "contract_version": {"type": "integer", "enum": [version]}}.items()))
            if spec.name == "research_plan" else spec for spec in new.frozen_specs)
        old = restore_snapshot(snapshot_description(replace(new, frozen_specs=specs)))
        delegate = ExecutionDelegate(session=session, research_tools=tools,
                                     frozen_specs={spec.name: spec for spec in old.frozen_specs})
        result, ok, _ = delegate._execute_research(ToolCall("research_plan", {}))
        assert ok, result.model_text
        project_result_observation(session, result.call, result, ok=ok, research_ledger=tools.ledger)
        session._memory_results["plan"] = result
        assert registered_plan(session)["schema_version"] == 2
        assert registered_plan(session)["submission_mode"] == mode
        assert current_scope(session).submission_mode == mode
    finally:
        store.close()
