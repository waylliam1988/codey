"""The checked report survives version changes without recompilation."""

import copy
import json
from dataclasses import replace

import pytest

from codey.knowledge.model import KnowledgeMutation
from codey.operations.kernel_contracts import KernelResult
from codey.operations.kernel_control import result_from_payload
from codey.providers.response_record import json_value
from codey.research.finding_report import FindingReport, compile_finding_report
from codey.research.issues import ResearchIssue
from tests.test_research_findings import URL, execute, report_args, setup


@pytest.mark.usefixtures("public_source_dns")
def test_same_url_keeps_two_exact_source_versions_and_rendering_is_last(tmp_path, monkeypatch):
    store, tools, session, delegate = setup(tmp_path)
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        first = execute(delegate, tools, session, "save_finding", {"claim": "Only one writer can write at a time.",
            "source_ref": "s1", "passage_ref": next(iter(tools.ledger.read_passages))})
        f1 = json.loads(first.model_text)["finding_ref"]
        tools.search.text = "Updated guide: concurrent writers are unsupported. Measure queue latency under your own workload."
        execute(delegate, tools, session, "open_url", {"url": URL})
        passage = next(ref for ref, row in tools.ledger.read_passages.items() if row.excerpt == tools.search.text)
        second = execute(delegate, tools, session, "save_finding", {"claim": "Measure queue latency under your own workload.",
            "source_ref": "s1", "passage_ref": passage})
        f2 = json.loads(second.model_text)["finding_ref"]
        args = report_args(f1)
        args["report_sections"]["evidence"].append(f"The updated guide requires workload measurements. [{f2}]")
        compiled = compile_finding_report(args, tools.ledger, store)
        assert [row["url"] for row in compiled.source_versions] == [URL, URL]
        assert len({(row["id"], row["revision"]) for row in compiled.source_versions}) == 2
        from codey.research.report_projection import project_accepted_report

        record = project_accepted_report(compiled, question="Compare versions", run_id="run", session_id="first",
                                         project="", synthesis_id="report")
        assert len(record.sources) == 2 and len(record.evidence) == 2
        assert all(row.evidence_refs for row in record.claims)
        assert "[1]" in compiled.text and "[2]" in compiled.text
        # A field defect is rejected before renderer invocation.
        monkeypatch.setattr("codey.research.finding_report.render_report", lambda *a: pytest.fail("rendered invalid proposal"))
        args["report_sections"]["evidence"][1] = "Unbound statement [f99]"
        with pytest.raises(ResearchIssue) as failure:
            compile_finding_report(args, tools.ledger, store)
        assert failure.value.path == "report_sections.evidence[1]"
        assert failure.value.allowed_refs == ("f1", "f2") and not failure.value.requires_acquisition
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_receipt_binding_and_accepted_snapshot_never_resolve_latest_versions(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        saved = execute(delegate, tools, session, "save_finding", {"claim": "Only one writer can write at a time.",
            "source_ref": "s1", "passage_ref": next(iter(tools.ledger.read_passages))})
        label = json.loads(saved.model_text)["finding_ref"]
        compiled = compile_finding_report(report_args(label), tools.ledger, store)
        payload = json_value(KernelResult(True, compiled.text, 3, "done", research_report=compiled.to_payload()))
        binding = tools.ledger.references[label]
        old = store.repository.read(binding.object_id, binding.revision)
        store.commit_knowledge("changed", [KnowledgeMutation(replace(old.value, content="New wording"), old.revision)])
        assert compile_finding_report(report_args(label), tools.ledger, store).knowledge_refs == compiled.knowledge_refs
        # Decode uses only the saved compilation, including coverage and versions.
        restored = result_from_payload(payload)
        assert FindingReport.from_payload(restored.research_report) == compiled
        corrupt = copy.deepcopy(payload)
        corrupt["research_report"]["knowledge_refs"][0]["revision"] += 1
        with pytest.raises(ValueError, match="digest mismatch"):
            result_from_payload(corrupt)
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("citation", ["[f999]", "[1]", "[f0]"])
def test_coverage_answer_citations_have_field_local_feedback(tmp_path, citation):
    store, tools, session, delegate = setup(tmp_path)
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        saved = execute(delegate, tools, session, "save_finding", {"claim": "Only one writer can write at a time.",
            "source_ref": "s1", "passage_ref": next(iter(tools.ledger.read_passages))})
        label = json.loads(saved.model_text)["finding_ref"]
        args = report_args(label)
        args["report_sections"]["coverage"][0]["answer"] += " " + citation
        with pytest.raises(ResearchIssue) as error:
            compile_finding_report(args, tools.ledger, store)
        assert error.value.path == "report_sections.coverage[0].answer"
        args["report_sections"]["coverage"][0]["answer"] = f"Limited to one writer [{label}]"
        assert "Limited to one writer [1]" in compile_finding_report(args, tools.ledger, store).text
        # User question text is not model-selected report citation syntax.
        tools.ledger.questions = ("What does the literal [f999] in this question mean?",)
        assert "literal [f999]" in compile_finding_report(args, tools.ledger, store).text
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_one_detail_read_and_working_view_include_original_evidence(tmp_path):
    from codey.research.context_view import build_research_context_view
    from tests.test_research_findings import TEXT

    store, tools, session, delegate = setup(tmp_path)
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        saved = execute(delegate, tools, session, "save_finding", {"claim": "Only one writer can write at a time.",
            "source_ref": "s1", "passage_ref": next(iter(tools.ledger.read_passages))})
        label = json.loads(saved.model_text)["finding_ref"]
        detail = execute(delegate, tools, session, "knowledge_read", {"id": label})
        value = json.loads(detail.model_text)
        assert value["finding_ref"] == label and value["evidence"][0]["excerpt"] == TEXT
        assert value["evidence"][0]["source_revision"] == 1 and value["evidence_complete"]
        view = build_research_context_view(session, tools.ledger, store, session_id="first")
        assert view.references[0]["excerpt"] == TEXT
        assert view.references[0]["interpretation_review"] == "unreviewed"
        assert not view.references[0]["dependencies_changed"]
    finally:
        store.close()
