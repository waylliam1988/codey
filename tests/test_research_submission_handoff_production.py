"""Exercise the frozen production Research contract against browser receipts."""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from codey.operations.task_execution import ExecutionDelegate
from codey.policies.tool_requirement import require_tool_success
from codey.providers.api_generation import AttemptObserver
from codey.research.answer_contract import AnswerScope, digest, submit_answer
from codey.research.ledger import OpenedSource
from codey.research.passages import ReadPassage
from codey.research.report_evidence import CLAIM_CHECK_VERSION, check_report_evidence, review_submission_issues
from codey.research.tools import ResearchTools
from codey.runtime.core.models import ToolCall, ToolResult
from codey.types.json_value import JsonObject, decode_json_object, parse_json_object
from tools import browser_prerequisite_ab as fixture
from tools import browser_production_live_gate as gate
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import ExperimentProvider, Target


class NativeExperimentProvider(ExperimentProvider):
    def __init__(self, target: Target, directory: Path, *, fixed_acquisition: bool = True,
                 acquisition_action: JsonObject | None = None, stream: bool = False) -> None:
        super().__init__(target, directory, fixed_acquisition=fixed_acquisition,
                         acquisition_action=acquisition_action, stream=stream)
        self.native_tools = True


def install_http(monkeypatch: pytest.MonkeyPatch, replies: Iterator[JsonObject], *, native: bool) -> None:
    call_id = 0

    def scripted(url: str, payload: dict[str, object], headers: dict[str, str], *,
                 observe: AttemptObserver, **kwargs: object) -> JsonObject:
        nonlocal call_id
        observe(attempt=1, data=json.dumps(payload).encode(), phase="request", response_bytes=0, seconds=0.0)
        reply = next(replies)
        message: JsonObject = {"role": "assistant", "content": json.dumps(reply), "tool_calls": []}
        if native:
            call_id += 1
            message.update(content=None, tool_calls=[{"id": f"call-{call_id}", "type": "function",
                "function": {"name": reply["tool"], "arguments": json.dumps(reply["args"])}}])
        return {"choices": [{"message": message, "finish_reason": "tool_calls" if native else "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    monkeypatch.setattr("codey.providers.api_generation.generate", scripted)
    def empty_service_state(_url: str) -> JsonObject:
        return {}

    monkeypatch.setattr("tools.completion_explanation_ab.service_state", empty_service_state)
    def empty_model_metadata(_url: str, _key: str) -> JsonObject:
        return {}

    monkeypatch.setattr("codey.providers.local_selection._metadata_json", empty_model_metadata)


def requests(directory: Path) -> list[JsonObject]:
    rows = [parse_json_object(line)
            for line in (directory / "provider.jsonl").read_text(encoding="utf-8").splitlines()]
    return [decode_json_object(row["payload"]) for row in rows if row.get("phase") == "request"]


def assert_collecting(directory: Path) -> None:
    request = requests(directory)[-1]
    assert '"codemode"' in json.dumps(request.get("response_format", request.get("tools")))
    assert "post-wait page evidence is ready" not in json.dumps(request["messages"])


def assert_submitting(directory: Path, *, sequence: bool = True) -> str:
    request = requests(directory)[-1]
    schema = json.dumps(request.get("response_format", request.get("tools")))
    assert "submit_research_answer" in schema
    assert '"codemode"' in schema  # Report readiness preserves other authorized work.
    messages = json.dumps(request["messages"], ensure_ascii=False)
    assert "QUARTZ_851" in messages
    pattern = (r"post-wait page evidence is ready\. Use submit_research_answer and cite \[(e\d+)\]" if sequence else
               r"declared wait/read obligations have formal evidence\. Use \[(e\d+)\]")
    handoff = re.search(pattern, messages)
    assert handoff is not None
    return handoff[1]


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize(("case", "native"), [
    ("ready", False), ("legacy_claim_receipt", False), ("early_read", False), ("partial_read", False), ("reload", False),
    ("old_citation", False), ("lost_on_submit", False), ("undeclared", False),
    ("ready", True), ("old_citation", True), ("lost_on_submit", True),
    ("workflow", False), ("workflow_early_read", False), ("workflow_partial_read", False),
    ("workflow_wrong_wait", False), ("workflow_later_click", False),
    ("workflow_reload", False), ("workflow_lost_on_submit", True),
])
def test_production_handoff_requires_citable_read_on_current_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, native: bool,
) -> None:
    directory = tmp_path / "run"
    if native:
        monkeypatch.setattr(gate, "ExperimentProvider", NativeExperimentProvider)
    workflow = case.startswith("workflow")
    scenario = case.removeprefix("workflow_")
    if scenario in {"lost_on_submit", "legacy_claim_receipt"}:
        original = ExecutionDelegate._submit_research_answer
        reloaded = False

        def submit(delegate: ExecutionDelegate, call: ToolCall, tools: ResearchTools) -> tuple[ToolResult, bool, int | None]:
            nonlocal reloaded
            outcome = original(delegate, call, tools)
            if scenario == "legacy_claim_receipt" and outcome[1]:
                result, _, exit_code = outcome
                evidence = decode_json_object(result.canonical["research_evidence_check"])
                assert evidence.pop("claim_check_version") == CLAIM_CHECK_VERSION
                assert isinstance(evidence.pop("claim_checks"), list)
                return replace(result, canonical={**result.canonical, "research_evidence_check": evidence}), True, exit_code
            if outcome[1] and not reloaded:
                assert delegate.browser_session is not None and delegate.browser_session.navigate(gate.URL).ok
                reloaded = True
            return outcome

        monkeypatch.setattr(ExecutionDelegate, "_submit_research_answer", submit)

    def replies() -> Iterator[JsonObject]:
        assert_collecting(directory)
        code = fixture.ROUND
        if scenario == "workflow":
            code = code.replace("ref:s.elements.find(e=>e.name==='Locality').ref",
                                "ref:'['+s.elements.find(e=>e.name==='Locality').ref+']'")
            code = code.replace("ref:s.elements.find(e=>e.name==='Load record').ref",
                                "ref:'['+s.elements.find(e=>e.name==='Load record').ref+']'")
        elif scenario == "early_read":
            code = fixture.NAVIGATE + fixture.LOAD + f"const old={fixture.READ};" + fixture.WAIT + "return old;"
        elif scenario == "partial_read":
            code = fixture.ROUND.replace(fixture.READ, "unwrap(await tools.browser_read({limit:32}))")
        elif scenario == "reload":
            code = fixture.ROUND.replace(f"return {fixture.READ};", f"const old={fixture.READ};")
            code += fixture.NAVIGATE + "return old;"
        elif scenario == "old_citation":
            code = fixture.NAVIGATE + f"const old={fixture.READ};" + fixture.LOAD + fixture.WAIT + f"return {fixture.READ};"
        elif scenario == "wrong_wait":
            code = fixture.ROUND.replace("'鱼丸'", "'福州'")
        elif scenario == "later_click":
            code = fixture.ROUND.replace(f"return {fixture.READ};", f"const previous={fixture.READ};") + (
                "const after=unwrap(await tools.browser_snapshot({}));"
                "unwrap(await tools.browser_click({ref:after.elements.find(e=>e.name==='Load record').ref}));"
                "return previous;")
        yield fixture.code_action(code)
        sample = {"early_read", "partial_read", "reload", "wrong_wait", "later_click"}
        if scenario in sample:
            assert_collecting(directory)
            if scenario == "reload":
                recovery = fixture.LOAD + fixture.WAIT
            else:
                recovery = fixture.WAIT if scenario in {"wrong_wait", "later_click"} else ""
            yield fixture.code_action(recovery + f"return {fixture.READ};")
        if case == "undeclared":
            assert_collecting(directory)
            yield fixture.submission("e2", current=True)
            return
        label = assert_submitting(directory, sequence=not workflow)
        if case == "old_citation":
            assert label != "e1"
            yield fixture.submission("e1", current=False)
            assert assert_submitting(directory, sequence=not workflow) == label
        if scenario == "lost_on_submit":
            yield fixture.submission(label, current=True)
            assert_collecting(directory)
            prefix = fixture.NAVIGATE if workflow else ""
            yield fixture.code_action(prefix + fixture.LOAD + fixture.WAIT + f"return {fixture.READ};")
            label = assert_submitting(directory, sequence=not workflow)
        yield fixture.submission(label, current=True)

    install_http(monkeypatch, replies(), native=native)
    required = (require_tool_success("browser_wait", via="codemode", arguments={"condition": "text", "value": "鱼丸"}),)
    if case == "workflow_wrong_wait":
        # Use the argument-qualified requirement in place of the bare wait declaration.
        result = gate.run_case(directory, "research", Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10),
                               strict_research=True, explicit_wait=False, additional_required_tools=required)
    else:
        result = gate.run_case(directory, "research", Target("http://scripted.test/v1", "scripted-fixture", 123456789, 10),
                               strict_research=True, research_sequence=not workflow and case != "undeclared",
                               explicit_wait=case != "undeclared")
    assert result["passed"] is True and result["stop_reason"] == "done", result


def test_new_claim_checks_only_cover_the_matching_part_and_cited_version() -> None:
    latest = "Test settings keep load stable. Revision 2 rejects high flow."
    prior = "Revision 1 supports high flow."
    url = "https://example.com/versioned"
    passages = {
        "new": ReadPassage("new", OpenedSource(url, url, "", "", "new"), latest, (), 0, len(latest),
                           source_version=("edition", 2)),
        "old": ReadPassage("old", OpenedSource(url, url, "", "", "old"), prior, (), 0, len(prior),
                           source_version=("edition", 1)),
    }
    scope = AnswerScope(digest("task"), (("e1", "new"), ("e2", "old")), ("attempt",))
    markdown = ("Test settings keep load stable; high flow mode never fails [e1].\n"
                "Revision 1 supports high flow [e1].\n"
                "Revision 1 supports high flow [e2].\n"
                "This conclusion applies to all previous versions.")
    submission = submit_answer(scope, {"state": "answered", "markdown": markdown})
    legacy = check_report_evidence(submission, passages)
    report = check_report_evidence(submission, passages, claim_check_version=1)
    assert legacy == {key: report[key] for key in legacy}
    checks = report["claim_checks"]
    assert isinstance(checks, list)
    rows = [decode_json_object(check) for check in checks]
    assert [row["review_state"] for row in rows] == ["source_text_match", "needs_review", "needs_review", "source_text_match", "uncited"]
    assert [markdown[int(str(row["report_start"])):int(str(row["report_end"]))] for row in rows] == [
        "Test settings keep load stable", "high flow mode never fails", "Revision 1 supports high flow",
        "Revision 1 supports high flow", "This conclusion applies to all previous versions",
    ]
    assert rows[0]["passage_refs"] == ["new"] and rows[3]["passage_refs"] == ["old"]
    assert rows[3]["source_spans"] == [{"passage_ref": "old", "start": 0, "end": len(prior) - 1,
                                       "source_version": ["edition", 1]}]


@pytest.mark.parametrize(("original", "claim", "problem"), [
    ("A 组响应率 24%。", "B 组响应率 24%", "group_conflict"),
    ("Group A response rate was 24%.", "B组响应率为24%", "group_conflict"),
    ("Group A response rate was 24%.", "A组响应率为24%", ""),
    ("A组响应率24%。B组响应率24%。", "B 组响应率为 24%", ""),
    ("A组响应率24%。B组响应率24%。", "C 组响应率为 24%", "group_conflict"),
    ("Group A shipped 24 kg. Group A shipped 24 g.", "Group A shipped 24.0 克", ""),
    ("A 组响应率 24%。", "A 组响应率 42%", "quantity_conflict"),
    ("Group A shipped 24 g.", "Group A shipped 24 kg", "quantity_conflict"),
    ("Group A shipped 24 g.", "Group A shipped 24.0 g", ""),
    ("Group A shipped 24 g.", "A 组发货 24 克", ""),
    ("Group A shipped 24 g.", "Group A's shipment weighed 24 g", ""),
    ("Group A shipped 24 g.", "Group A shipped 24 g", ""),
    ("Group A received 24 ml.", "Group A received 24 mL", ""),
    ("Group A received 24 ml.", "Group A received 42 mL", "quantity_conflict"),
    ("Group A received 24 ml.", "Group B received 24 mL", "group_conflict"),
])
def test_review_flags_only_aligned_quantitative_conflicts(original: str, claim: str, problem: str) -> None:
    url = "https://example.com/official"
    passage = ReadPassage("p1", OpenedSource(url, url, "", "", "hash"), original, (), 0, len(original))
    passages = {passage.ref: passage}
    scope = AnswerScope(digest("task"), (("e1", passage.ref),), ("attempt",))
    submission = submit_answer(scope, {"state": "answered", "markdown": f"{claim} [e1]."})
    report = check_report_evidence(submission, passages, claim_check_version=CLAIM_CHECK_VERSION)
    issues = review_submission_issues(submission, report, passages)
    if problem:
        issue, = issues
        assert issue["kind"] == problem and issue["claim"] == claim
        assert issue["passage_ref"] == passage.ref and issue["label"] == "e1"
    else:
        assert issues == []


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
def test_quantitative_citation_conflict_returns_to_current_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "run"
    url = "https://example.com/atlas/shipping-results"
    original = "Group A shipped 24 g. The Atlas scale was calibrated before the comparison."

    def replies() -> Iterator[JsonObject]:
        assert "Group A shipped 24 g" in json.dumps(requests(directory)[-1]["messages"])
        yield {"tool": "submit_research_answer", "args": {"markdown": "Group B shipped 24 g [e1].", "state": "answered"}}
        feedback = json.dumps(requests(directory)[-1]["messages"])
        assert "The draft says 'Group B shipped 24 g'" in feedback and "Its cited [e1] says 'Group A shipped 24 g'" in feedback
        yield {"tool": "submit_research_answer", "args": {"markdown": "Group A shipped 24 g [e1].", "state": "answered"}}

    install_http(monkeypatch, replies(), native=False)
    result = gate.run_case(directory, "research", Target("http://scripted.test/v1", "fixture", 123, 20),
        strict_research=True, declare_required_tools=False, source_pages={url: f"<article>{original}</article>"},
        task_transform=lambda _: f"Read the official shipping report at {url}. Report which group shipped 24 g and cite the report.")
    assert result["stop_reason"] == "done"
    receipts = tool_receipts(directory / "state")
    submissions = [row.result for row in receipts if row.result.call.name == "submit_research_answer"]
    assert [row.ok for row in submissions] == [False, True]
    assert decode_json_object(submissions[0].canonical["research_submission_issue"])["kind"] == "group_conflict"
    assert sum(row.result.call.name == "open_url" for row in receipts) == 1


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
def test_repeated_citation_conflict_publishes_one_unresolved_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "run"
    url = "https://example.com/atlas/shipping-results"
    proposal: JsonObject = {"tool": "submit_research_answer",
        "args": {"markdown": "Group B shipped 24 g [e1].", "state": "answered"}}
    install_http(monkeypatch, iter((proposal, proposal, proposal)), native=False)
    result = gate.run_case(directory, "research", Target("http://scripted.test/v1", "fixture", 123, 20),
        strict_research=True, declare_required_tools=False, source_pages={url: "<article>Group A shipped 24 g.</article>"},
        task_transform=lambda _: f"Read the official shipping report at {url}. Report which group shipped 24 g and cite the report.")
    assert result["stop_reason"] == "done"
    receipts = tool_receipts(directory / "state")
    submissions = [row.result for row in receipts if row.result.call.name == "submit_research_answer"]
    assert [row.ok for row in submissions] == [False, False, True]
    delivered = decode_json_object(submissions[-1].canonical["research_submission"])
    assert delivered["state"] == "unresolved" and "Group B shipped" not in str(delivered["markdown"])
