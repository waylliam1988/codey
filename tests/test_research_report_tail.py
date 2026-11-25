"""Unfinished reports are repaired from existing evidence, never auto-completed."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from codey.knowledge.model import canonical_json
from codey.research.answer_contract import (
    AnswerScope,
    digest,
    submit_answer,
    validate_submission,
)
from codey.research.answer_fields import AnswerField
from codey.types.json_value import JsonObject, parse_json_object
from codey.utils.citation_scanner import mask_markdown_code
from tests.test_research_submission_handoff_production import (
    NativeExperimentProvider,
    assert_submitting,
    install_http,
    requests,
)
from tools import browser_prerequisite_ab as fixture
from tools import browser_production_live_gate as gate
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target

TASK = "Report the original inspected record."


def scope() -> AnswerScope:
    return AnswerScope(digest(TASK), (("e1", "p" + "b" * 12),), ("original-read",))


@pytest.mark.parametrize("tail", ["`", "``", "\\\\`", "`\n", "`` \n"])
def test_exposed_terminal_code_delimiter_cannot_be_submitted(tail: str) -> None:
    with pytest.raises(ValueError, match="report_tail_incomplete") as caught:
        submit_answer(scope(), {"markdown": "Actual record [e1].\n- 将 code 更新为完整字符串 " + tail, "state": "answered"})
    assert "Repair only" in str(caught.value) and "Do not repeat successful" in str(caught.value)


@pytest.mark.parametrize("tail", [
    "`QUARTZ_851`", "``a`b``", "\\`", "\\\\\\`", "\\\\`QUARTZ_851`", "`literal \\`",
    "No final punctuation", "Heading:", "前提尚不确定", "\n```text\n`\n```", "\n    `",
])
def test_complete_code_literal_escapes_and_plain_prose_are_preserved(tail: str) -> None:
    markdown = "Actual record [e1].\n" + tail
    submission = submit_answer(scope(), {"markdown": markdown, "state": "partial"})
    assert submission["markdown"] == markdown
    assert validate_submission(submission, TASK) == submission


@pytest.mark.parametrize("version", [4, 5, 6, 7, 8, 9, 10])
def test_retired_scope_versions_are_rejected(version: int) -> None:
    payload = parse_json_object(canonical_json(scope().to_payload()))
    payload["schema_version"] = version
    with pytest.raises(ValueError, match="invalid whole-task research scope"):
        AnswerScope.from_payload(payload)


def test_field_scope_still_requires_a_complete_report() -> None:
    frozen = replace(scope(), fields=(AnswerField("/code", "Record code", "QUARTZ_851", ("e1",), "", False),))
    args: JsonObject = {"markdown": "Recorded original source.\n{{field:/code}}\nUnfinished literal `", "state": "answered"}
    with pytest.raises(ValueError, match="report_tail_incomplete"):
        submit_answer(frozen, args)
    with pytest.raises(ValueError, match="Submit markdown"):
        submit_answer(scope(), {**args, "scope": parse_json_object(canonical_json(frozen.to_payload()))})


def test_layout_checks_handle_escaped_backslashes_without_changing_historical_offsets() -> None:
    markdown = r"A literal backslash followed by code: \\`[e999]` and actual source [e1]."
    assert "[e999]" in mask_markdown_code(markdown)
    masked = mask_markdown_code(markdown, escaped_backslashes=True)
    assert len(masked) == len(markdown)
    assert "[e999]" not in masked and "[e1]" in masked


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("native", [False, True])
def test_production_rejects_unfinished_report_then_publishes_repair_without_reacquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool,
) -> None:
    directory = tmp_path / "run"
    if native:
        monkeypatch.setattr(gate, "ExperimentProvider", NativeExperimentProvider)
    def replies() -> Iterator[JsonObject]:
        yield fixture.code_action(fixture.ROUND)
        label = assert_submitting(directory)
        yield {"tool": "submit_research_answer", "args": {
            "markdown": f"Actual record: 173 grams; QUARTZ_851 [{label}].\n- 将 code 更新为完整字符串 `", "state": "answered"}}
        feedback = canonical_json(requests(directory)[-1]["messages"])
        assert "report_tail_incomplete" in feedback
        assert "Do not repeat successful" in feedback
        yield {"tool": "submit_research_answer", "args": {
            "markdown": f"Actual record: 173 grams; `QUARTZ_851` [{label}]. The requested record is verified.", "state": "answered"}}
    install_http(monkeypatch, replies(), native=native)
    result = gate.run_case(directory, "research", Target("http://scripted.test/v1", "fixture", 123456789, 10),
        strict_research=True, research_sequence=True, restore_snapshots=True, max_turns=4)
    assert result["stop_reason"] == "done" and result["passed"] is True, result
    receipts = [row.result for row in tool_receipts(directory / "state")]
    reports = [row for row in receipts if row.call.name == "submit_research_answer"]
    assert [row.ok for row in reports] == [False, True]
    assert "research_submission" not in reports[0].canonical
    assert "repair" not in reports[1].call.args  # The original tool performs the replacement submission.
    assert sum(row.call.name == "browser_fill" for row in receipts) == 1
    assert sum(row.call.name == "browser_click" for row in receipts) == 1
    first_submission = next(index for index, row in enumerate(receipts) if row.call.name == "submit_research_answer")
    assert any(row.call.name == "browser_read" and row.ok for row in receipts[:first_submission])
    assert not any(row.call.name in {"browser_read", "browser_navigate", "open_url", "source_search"}
                   for row in receipts[first_submission:])
