"""Citation and coverage bindings survive removal of the obsolete assessor."""

import pytest

from codey.research.finding_report import compile_finding_report
from tests.test_research_findings import URL, answer_args, execute, report_args, setup


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
    finally:
        store.close()


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("inline", [False, True])
def test_compiled_coverage_findings_bind_the_rendered_answer(tmp_path, inline):
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
        answer = next(row for row in report.paragraph_bindings
                      if row["path"] == "report_sections.coverage[0].answer")
        assert answer["refs"] == ["f1"]
        # Structured findings must still reject a conflicting inline reference.
        coverage["answer"] += " [f2]"
        execute(delegate, tools, session, "save_finding", {
            "claim": "Readers and writers can run concurrently.", "source_ref": "s1",
            "passage_ref": next(iter(tools.ledger.read_passages))})
        with pytest.raises(ValueError, match="answer citations must also occur"):
            compile_finding_report(args, tools.ledger, store)
    finally:
        store.close()
