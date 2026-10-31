"""An absent counterargument is a run observation, not invented source content."""

import pytest

from codey.research.finding_report import compile_finding_report
from codey.research.semantic_review import review_basis
from codey.research.tool_contract import done_spec_for_purpose
from codey.toolchain.tool_spec import spec_for_tool, validate_args_with_spec
from tests.test_research_findings import URL, execute, report_args, selection_args, setup


@pytest.mark.usefixtures("public_source_dns")
def test_empty_counter_compiles_without_creating_an_unsupported_claim(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        execute(delegate, tools, session, "save_finding", {
            "claim": "Only one writer can write at a time.",
            "passage_ref": next(iter(tools.ledger.read_passages))})
        args = report_args("f1")
        args["report_sections"]["counter"] = []
        current = selection_args("f1")
        current["report_sections"]["coverage"][0]["state"] = "evidence_found"
        assert not validate_args_with_spec(done_spec_for_purpose(spec_for_tool("done"), "report"), current)
        assert "本轮未记录额外反证" in compile_finding_report(current, tools.ledger, store).text
        report = compile_finding_report(args, tools.ledger, store)
        assert "本轮未记录额外反证" in report.text
        basis = review_basis(report, tools, "Explain writer concurrency.")
        assert not any("counter" in row["path"] for row in basis["paragraphs"])
        assert "Only one writer" in report.reference_metadata["f1"]["claim"]
    finally:
        store.close()
