"""Parent coverage keeps every unresolved relationship in the original plan."""

import json

import pytest

from codey.research.local_scope import plan_units, question_details
from codey.research.task_requirements import coverage_requirements
from tests.test_research_deterministic_completion import _run


@pytest.mark.usefixtures("public_source_dns")
@pytest.mark.parametrize("kind", ["versions", "checkpoint"])
@pytest.mark.parametrize("native", [False, True])
def test_parent_is_partial_with_explicit_missing_relation_and_replays(tmp_path, kind, native):
    if kind == "versions":
        question = "When was WAL introduced and when may a read-only WAL database open?"
        targets = ["WAL introduction version", "Readonly support version, all OR alternatives and exceptions"]
        text = "Beginning with SQLite version 3.7.0, the WAL option is available."
        claim = "SQLite WAL was introduced in version 3.7.0."
    else:
        question = "When must checkpoint stop, and how does it resume?"
        targets = ["Checkpoint stop boundary and reason", "Checkpoint progress recording and next invocation transfer"]
        text = "A checkpoint must stop past any current reader end mark to avoid overwriting pages in use by that reader."
        claim = "Checkpoint stops past any current reader end mark, to avoid overwriting database pages used by that reader."

    task_refs = [row["task_ref"] for row in coverage_requirements(question)]

    def factory(provider):
        original = provider.send

        def send(prompt):
            row = json.loads(original(prompt))
            events = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
            turn = next(event["turn"] for event in reversed(events) if "turn" in event)
            if turn == 1:
                row["args"] = {"questions": [question], "question_details": [{"task_refs": task_refs, "work_units": targets}]}
            elif turn == 4:
                row["args"]["claim"] = claim
            elif turn in (5, 6):
                row = {"tool": "submit_research_answer", "args": {
                    "state": "answered" if turn == 5 else "unresolved", "findings": ["f1"] if turn == 5 else []}}
            elif turn > 6:
                pytest.fail(prompt[-1500:])
            return json.dumps(row)

        provider.send = send
        return provider

    result = _run(tmp_path, native=native, task=question, source_text=text, provider_factory=factory)
    assert result.stop_reason == "done", result.summary
    report = result.accepted_report
    assert report.questions == (question,) and len(report.coverage) == 1
    assert report.coverage[0]["state"] == "partial"
    assert report.coverage[0]["findings"] == ["f1"] and report.coverage[0]["task_refs"] == task_refs
    assert claim in report.coverage[0]["answer"] and targets[1] in report.coverage[0]["answer"]
    assert targets[1] in report.text and len(report.selection_receipts) == 2
    assert _run(tmp_path, native=native, task=question, source_text=text, provider_factory=factory, resume=True) == result


def test_units_are_program_numbered_and_requirements_are_registered_once():
    task = "Answer versions. Preserve every OR alternative."
    details = [{"task_refs": ["r1", "r2"], "work_units": ["introduction", "readonly version and complete OR list"]}]
    frozen = question_details(details, ("versions",), task)
    units = plan_units({"questions": ["versions"], "question_details": frozen})
    assert [row["unit_ref"] for row in units] == ["q1.u1", "q1.u2"]
    assert all(row["task_refs"] == ["r1", "r2"] and row["question"] == "versions" for row in units)
    assert units[1]["target"].endswith("complete OR list")
    for bad in ([], ["same", "same"], ["x"] * 5):
        with pytest.raises(ValueError, match="work_units"):
            question_details([{**details[0], "work_units": bad}], ("versions",), task)
