"""One exact saved finding supplies report views without a final paraphrase."""

import json
from dataclasses import replace

import pytest

from codey.operations.kernel_protocol import build_turn_snapshot, normalize_turn
from codey.protocols.text_turn import decode_text_turn
from codey.providers.base import AssistantTurn, ProviderToolCall
from codey.research.finding_report import FindingReport, compile_finding_report
from codey.research.semantic_review import review_basis
from tests.test_research_findings import URL, execute, report_args, setup


@pytest.mark.usefixtures("public_source_dns")
def test_one_answer_compiles_and_restores_without_a_second_authored_report(tmp_path):
    store, tools, session, delegate = setup(tmp_path)
    try:
        execute(delegate, tools, session, "open_url", {"url": URL})
        execute(delegate, tools, session, "save_finding", {"claim": "Only one writer can write at a time.",
            "passage_ref": next(iter(tools.ledger.read_passages))})
        args = {"report_sections": {"coverage": [{key: value for key, value in row.items() if key != "answer"}
            for row in report_args("f1")["report_sections"]["coverage"]]}}
        args["report_sections"]["coverage"][0].update(state="evidence_found")
        session.policy = replace(session.policy, strict_research=True)
        snapshot = build_turn_snapshot(session)
        for reply in (decode_text_turn(json.dumps({"tool": "done", "args": args})),
                      AssistantTurn(tool_calls=(ProviderToolCall("done", "done", args),))):
            assert not normalize_turn(reply, snapshot=snapshot).protocol_error
        assert normalize_turn(decode_text_turn(json.dumps({"tool": "done", "args": report_args("f1")})),
                              snapshot=snapshot).protocol_error
        report = compile_finding_report(args, tools.ledger, store)
        assert report.text.count("Only one writer can write at a time.") == 3
        assert FindingReport.from_payload(report.to_payload()).to_payload() == report.to_payload()
        basis = review_basis(report, tools, "Explain the writer constraint.")
        assert [row["path"] for row in basis["paragraphs"]] == ["report_sections.coverage[0].answer"]
        assert basis["paragraphs"][0]["refs"] == ["f1"]
        legacy = {"report_sections": {"coverage": report_args("f1")["report_sections"]["coverage"]}}
        for invalid in ("wrong [1]", "unknown [f77]"):
            legacy["report_sections"]["coverage"][0]["answer"] = invalid
            with pytest.raises(ValueError):
                compile_finding_report(legacy, tools.ledger, store)
    finally:
        store.close()
