"""Question ownership follows the original execution receipts through recovery."""

import copy
import json
from dataclasses import replace

import pytest

from codey.operations.kernel_facts import project_result_observation
from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.protocols.text_turn import decode_text_turn
from codey.research.finding_report import compile_finding_report
from codey.research.ledger import ResearchLedger
from codey.research.questions import INVESTIGATION_TOOLS, validate_question_coverage
from codey.runtime.core.models import ToolCall
from tests.test_research_findings import URL, execute, report_args, setup


@pytest.mark.usefixtures("public_source_dns")
def test_passage_reuse_preserves_acquisition_and_adoption_ownership_on_recovery(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        plan = execute(delegate, tools, session, "research_plan", {
            "questions": ["What limits writer concurrency?", "What limits checkpoint progress?"]})
        opened = execute(delegate, tools, session, "open_url", {"url": URL, "question_ref": "q1"})
        passage = next(iter(tools.ledger.read_passages))
        finding_args = {"claim": "Only one writer can write at a time.", "source_ref": "s1", "passage_ref": passage}
        saved = execute(delegate, tools, session, "save_finding", {**finding_args, "question_ref": "q1"})
        assert saved.ok
        before = store.index.count()
        reused = execute(delegate, tools, session, "save_finding", {
            "claim": finding_args["claim"], "passage_ref": passage, "question_ref": "q2"})
        assert reused.ok and json.loads(reused.model_text)["finding_ref"] == "f2"
        assert store.index.count() == before
        assert len(tools.ledger.investigations) == 1
        assert tools.ledger.investigations["a1"]["question_ref"] == "q1"
        coverage = report_args("f1")["report_sections"]["coverage"]
        coverage.append({"question_ref": "q2", "state": "not_investigated", "answer": "Not investigated.",
                         "findings": [], "investigations": []})
        coverage[0]["investigations"] = ["a1"]
        assert validate_question_coverage(coverage, tools.ledger)
        corrupt = copy.deepcopy(coverage)
        corrupt[0]["investigations"] = []
        corrupt[1].update(state="unanswered_after_search", investigations=["a1"])
        with pytest.raises(ValueError, match="belongs to another question"):
            validate_question_coverage(corrupt, tools.ledger)
        corrupt[1].update(state="partial", findings=["f1"], investigations=[])
        with pytest.raises(ValueError, match="finding f1 belongs to another question"):
            validate_question_coverage(corrupt, tools.ledger)
        recovered, restored_session = ResearchLedger(), TaskSession(session.policy)
        for result in (plan, opened, saved, reused):
            project_result_observation(restored_session, result.call, result, ok=True, research_ledger=recovered)
        assert recovered.investigations == tools.ledger.investigations
        assert recovered.references == tools.ledger.references
        assert recovered.investigations["a1"]["passage_refs"] == [passage]
        assert validate_question_coverage(coverage, recovered)
        # An explicit read for q2 creates a second adoption, not a second knowledge write.
        read = execute(delegate, tools, session, "knowledge_read", {"id": "f1", "question_ref": "q2"})
        assert read.ok and json.loads(read.model_text)["finding_ref"] == "f2"
        assert tools.ledger.references["f2"].object_id == tools.ledger.references["f1"].object_id
        assert tools.ledger.references["f2"].acquisition == "current_run"
        assert tools.ledger.references["f2"].note_id == tools.ledger.references["f1"].note_id
        assert tools.ledger.references["f2"].passage_ref == passage
        assert store.repository.read(tools.ledger.references["f1"].object_id).revision == 1
        inference = {"statement": "A second writer must wait for the first writer.",
                     "premise_refs": ["f1"], "limitations": "A candidate scheduling inference.", "question_ref": "q2"}
        refused = execute(delegate, tools, session, "save_inference", inference)
        assert not refused.ok and "premise belongs to another question" in refused.model_text
        saved_inference = execute(delegate, tools, session, "save_inference", {**inference, "premise_refs": ["f2"]})
        assert saved_inference.ok and tools.ledger.references["f3"].question_ref == "q2"
        proposal = report_args("f1")
        proposal["report_sections"]["coverage"] = coverage
        compiled = compile_finding_report(proposal, tools.ledger, store)
        assert compiled.reference_metadata["f1"]["question_ref"] == "q1"
        assert {ref for row in compiled.coverage for ref in row["investigations"]} == {"a1"}
        assert len(tools.ledger.investigations) > 1
    finally:
        store.close()


def test_source_search_alias_preserves_question_and_enforces_its_frozen_requirement():
    from codey.toolchain.tool_spec import validate_args_with_spec

    session = TaskSession(TaskPolicy(
        frozenset({"web.read", "knowledge.read", "control"}), strict_research=True))
    session.research_questions = ("Where is the restriction?",)
    session.opened_sources = {URL}
    session.source_ids = {URL: "s1"}
    snapshot = build_turn_snapshot(session)
    spec = next(spec for spec in snapshot.frozen_specs if spec.name == "source_search")
    valid = {"source_id": "s1", "query": "writer", "question_ref": "q1"}
    assert not validate_args_with_spec(spec, valid)
    for bad in ({"source_id": "s1", "query": "writer"}, {**valid, "question_ref": "q9"}):
        assert validate_args_with_spec(spec, bad)
    plan = normalize_turn(decode_text_turn(json.dumps({"tool": "source_search", "args": valid})), snapshot=snapshot)
    assert not plan.protocol_error and plan.calls[0].args == valid


def test_question_argument_is_the_same_contract_in_direct_and_kernel_tool_projections():
    from codey.research.tool_contract import TOOL_CONTRACTS, openai_parameters_schema
    from codey.toolchain.tool_spec import spec_for_tool

    for name in INVESTIGATION_TOOLS | {"save_finding", "save_inference"}:
        if name not in TOOL_CONTRACTS:
            continue
        direct = openai_parameters_schema(name)
        spec = spec_for_tool(name)
        assert direct["properties"]["question_ref"] == dict(spec.parameters)["question_ref"]
        assert json.loads(spec.json_examples[0])["args"]["question_ref"] == "q1"


@pytest.mark.parametrize("evidence_only", [False, True])
@pytest.mark.parametrize("name", sorted(INVESTIGATION_TOOLS | {"save_finding", "save_inference"}))
def test_strict_snapshot_requires_question_on_all_investigation_and_adoption_tools(tmp_path, name, evidence_only):
    store, tools, session, delegate = setup(tmp_path)
    try:
        session.policy = replace(session.policy, strict_research=not evidence_only)
        if evidence_only:
            session.task_kind = "evidence_followup"
            session.research_acquisition_limits = (3, 6)
        session.search_results = {"r1": URL}
        session.source_ids = {URL: "s1"}
        session.opened_sources = {URL}
        session.hit_targets = {"h1": {"url": URL}}
        session.evidence = [(URL, "one writer")]
        snapshot = build_turn_snapshot(session)
        spec = next(spec for spec in snapshot.frozen_specs if spec.name == name)
        assert "question_ref" in spec.required
        done = next(spec for spec in snapshot.frozen_specs if spec.name == "done")
        assert ("report_sections" in done.required) is not evidence_only
        example = json.loads(spec.json_examples[0])
        example["args"].pop("question_ref")
        assert normalize_turn(decode_text_turn(json.dumps(example)), snapshot=snapshot).protocol_error
        # Even a direct executor call cannot execute an unbound strict action.
        result, ok, _ = delegate._execute_research(ToolCall(name, example["args"]))
        assert not ok and "question_ref" in result.model_text
        project_result_observation(session, result.call, result, ok=False, research_ledger=tools.ledger)
        assert not tools.ledger.investigations
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_source_quality_has_only_pinned_metadata_and_never_infers_latest_from_fetch_time(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        execute(delegate, tools, session, "save_finding", {"claim": "Only one writer can write at a time.",
            "source_ref": "s1", "passage_ref": next(iter(tools.ledger.read_passages))})
        args = report_args("f1")
        compiled = compile_finding_report(args, tools.ledger, store)
        assert '"release_version":"unknown"' in compiled.text
        assert '"publication_date":"unknown"' in compiled.text
        assert not any("source_quality" in row["path"] for row in compiled.paragraph_bindings)
        args["report_sections"]["source_quality"] = ["All sources are the current stable release."]
        with pytest.raises(ValueError, match="source quality is generated"):
            compile_finding_report(args, tools.ledger, store)
    finally:
        store.close()
