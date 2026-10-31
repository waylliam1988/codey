"""Diagnostic assessment scope; model judgments have no completion authority."""

import copy
import json

import pytest

from codey.research.finding_report import compile_finding_report
from codey.research.semantic_review import parse_review, review_basis, review_issues, review_scope
from tests.test_research_findings import URL, answer_args, execute, report_args, setup

TASK = "Explain writer concurrency. Distinguish historical version restrictions."


@pytest.mark.usefixtures("public_source_dns")
def test_inline_labels_do_not_duplicate_rendered_citations_or_rewrite_saved_findings(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        claim = "Only one writer can write at a time. [f999]"
        execute(delegate, tools, session, "save_finding", {"claim": claim,
            "passage_ref": next(iter(tools.ledger.read_passages))})
        args = answer_args("f1")
        args["report_sections"]["coverage"][0]["answer"] = "Only one writer can write at a time. [f1]"
        report = compile_finding_report(args, tools.ledger, store)
        assert "[f999]" not in report.text and "[1] [1]" not in report.text
        assert report.coverage[0]["findings"] == ["f1"]
        assert report.reference_metadata["f1"]["claim"] == claim
        basis = review_basis(report, tools, "Explain writer concurrency.")
        assert basis["claims"][0]["claim"] == claim
        assert len(basis["claims"][0]["units"]) == 1
        assert len(basis["claims"][0]["evidence"]) == 1
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("inline", [False, True])
def test_compiled_coverage_findings_bind_the_rendered_answer_for_review(tmp_path, inline):
    store, tools, session, delegate = setup(tmp_path)
    try:
        tools.search.text = "Only one writer at a time. Readers and writers can run concurrently."
        execute(delegate, tools, session, "open_url", {"url": URL})
        execute(delegate, tools, session, "save_finding", {
            "claim": "Only one writer can write at a time.", "source_ref": "s1",
            "passage_ref": next(iter(tools.ledger.read_passages))})
        args = report_args("f1")
        coverage = args["report_sections"]["coverage"][0]
        coverage.update(state="partial", answer="Only one writer can write at a time." + (" [f1]" if inline else ""))
        report = compile_finding_report(args, tools.ledger, store)
        basis = review_basis(report, tools, "Explain writer concurrency.")
        answer = next(row for row in basis["paragraphs"] if row["path"] == "report_sections.coverage[0].answer")
        assert answer["refs"] == ["f1"] and answer["kinds"] == ["claim"]
        raw = assessment(basis)
        assert parse_review(json.dumps(raw), basis)["paragraphs"][-1]["units"][0]["finding_units"] == [
            {"ref": "f1", "unit": 1}]
        # Structured findings must still reject a conflicting inline reference.
        coverage["answer"] += " [f2]"
        execute(delegate, tools, session, "save_finding", {
            "claim": "Readers and writers can run concurrently.", "source_ref": "s1",
            "passage_ref": next(iter(tools.ledger.read_passages))})
        with pytest.raises(ValueError, match="answer citations must also occur"):
            compile_finding_report(args, tools.ledger, store)
    finally:
        store.close()


def assessment(basis, reject=False):
    requirements = []
    paths = basis.get("report_paths", [row["path"] for row in basis["paragraphs"]])
    for row in basis["requirements"]:
        historical = "historical version restrictions" in row["text"]
        requirements.append({"task_ref": row["task_ref"],
            "question_refs": ([] if reject else ["q2"]) if historical else ["q1"],
            "report_paths": ([] if reject else [paths[-1]]) if historical else [paths[0]],
            "state": ("missing" if reject else "acknowledged_gap") if historical else "addressed",
            "reason": "Version evidence is missing." if historical else "Explicit writer limit."})
    return {"requirements": requirements,
        "investigations": [{"ref": ref, "question_ref": row["question_ref"], "verdict": "relevant",
            "reason": "The investigation targets the registered question."}
            for row in basis["coverage"] for ref in row["investigations"]
            if ref in basis.get("investigation_scope", [ref])],
        "claims": [{"ref": row["ref"], "units": [{"unit": unit["unit"],
                    "verdict": "unsupported" if reject else "supported",
                    "quotes": [] if reject else [{"evidence": 1, "quote": "Only one writer at a time."}],
                    "reason": "The excerpt permits only one writer; concurrent writers are unsupported." if reject else
                              "The excerpt explicitly permits only one writer."} for unit in row["units"]]}
                   for row in basis["claims"]],
        "paragraphs": [{"path": row["path"], "units": [{"unit": unit["unit"],
            "verdict": "unsupported" if reject else "supported",
            "basis": "findings" if row["refs"] else "gap",
            "finding_units": [{"ref": ref, "unit": 1} for ref in row["refs"]],
            "reason": "Check writer limit and stated gaps."} for unit in row["units"]]}
            for row in basis["paragraphs"]]}


@pytest.mark.usefixtures("public_source_dns")
def test_reported_conditions_cannot_disappear_from_the_frozen_review_scope(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        execute(delegate, tools, session, "save_finding", {
            "claim": "Only one writer can write at a time.", "source_ref": "s1",
            "passage_ref": next(iter(tools.ledger.read_passages)), "conditions": [
                {"name": "host", "state": "reported", "value": "same host only"},
                {"name": "workload", "state": "not_extracted"}]})
        report = compile_finding_report(report_args("f1"), tools.ledger, store)
        basis = review_basis(report, tools, "Explain writer concurrency.")
        assert review_scope(report, basis["task"])["claims"][0]["units"] == basis["claims"][0]["units"]
        units = basis["claims"][0]["units"]
        assert len(units) == 2 and units[-1]["field"] == "conditions.host"
        assert "same host only" in units[-1]["text"]
        raw = assessment(basis, reject=True)
        raw["claims"][0]["units"].pop()
        with pytest.raises(ValueError, match="every frozen claim unit"):
            parse_review(json.dumps(raw), basis)
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
def test_review_uses_full_exact_excerpt_and_rejects_missing_duplicate_or_forged_judgments(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        tools.search.text = "Context without the essential condition. " * 20 + "Only one writer at a time."
        execute(delegate, tools, session, "open_url", {"url": URL})
        saved = execute(delegate, tools, session, "save_finding", {"claim": "Only one writer can write at a time.",
            "source_ref": "s1", "passage_ref": next(iter(tools.ledger.read_passages))})
        label = json.loads(saved.model_text)["finding_ref"]
        report = compile_finding_report(report_args(label), tools.ledger, store)
        assert report.evidence_bindings[0]["display_truncated"]
        basis = review_basis(report, tools, TASK)
        assert basis["claims"][0]["evidence"][0]["excerpt"].endswith("Only one writer at a time.")
        assert "claim" not in basis["claims"][0]["evidence"][0]
        assert "conditions" not in basis["claims"][0]["evidence"][0]
        raw = assessment(basis, reject=True)
        parsed = parse_review(json.dumps(raw), basis)
        assert {row["kind"] for row in review_issues(parsed)} == {"task_coverage", "claim_support", "report_scope"}
        assert review_issues(parsed)[0]["task_ref"] == "r2"
        for mutation in (lambda row: row["claims"].clear(),
                         lambda row: row["paragraphs"].append(row["paragraphs"][0]),
                         lambda row: row["requirements"][0].update(task_ref="invented task requirement"),
                         lambda row: row["requirements"][0].update(question_refs=[]),
                         lambda row: row["requirements"].pop(),
                         lambda row: row["requirements"].__setitem__(1, row["requirements"][0]),
                         lambda row: row["requirements"][0].update(kind="delivery"),
                         lambda row: row["claims"][0]["units"][0].update(verdict="probably"),
                         lambda row: row["claims"][0]["units"].clear()):
            corrupt = copy.deepcopy(raw)
            mutation(corrupt)
            with pytest.raises(ValueError):
                parse_review(json.dumps(corrupt), basis)
    finally:
        store.close()
