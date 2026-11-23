from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from itertools import permutations
from pathlib import Path

import pytest

from codey.codemode.catalog import snapshot_catalog
from codey.codemode.host import CodeModeHost
from codey.operations.kernel_protocol import build_turn_snapshot, offer_browser_tools
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.research.answer_contract import AnswerScope, digest, submit_answer
from codey.research.evidence_gaps import conflict_search
from codey.research.ledger import EvidenceItem, OpenedSource, ResearchLedger
from codey.research.passages import ReadPassage, read_passage_refs
from codey.research.reference import ResearchReference
from codey.research.report_evidence import check_report_evidence
from codey.runtime.core.models import ToolCall, ToolResult
from codey.task.web_intent import source_url_key
from codey.types.json_value import JsonObject, ToolArgs, decode_json_object
from tests.test_browser_result_adoption_ab import DOCUMENT, session_with_snapshot
from tests.test_research_submission_handoff_production import NativeExperimentProvider, install_http
from tools import research_query_delivery_live_gate as gate
from tools.browser_experiment_sources import ResumableBrowserSearchProvider
from tools.completion_artifact_audit import tool_receipts
from tools.completion_explanation_ab import Target
from tools.followup_research_live import BIRCH, CASE, CEDAR


def test_browser_flow_uses_bound_url_and_validates_every_step_before_dispatch() -> None:
    url = "https://example.org/record?id=A%2FB#item"
    session = TaskSession(TaskPolicy(frozenset({"control", "web.read", "browser.observe", "browser.interact", "browser.effect"})),
        task_text=f"Open <{url}> in the browser.", required_source_urls=frozenset({source_url_key(url)}), task_kind="chat")
    snapshot = offer_browser_tools(build_turn_snapshot(session), session)
    calls: list[ToolCall] = []

    def execute(call: ToolCall) -> ToolResult:
        calls.append(call)
        return ToolResult(call, "navigation unavailable", ok=False)

    entries = snapshot_catalog(snapshot).entries
    host = CodeModeHost(execution_id="flow", catalog=entries, execute=execute, allow_writes=True,
                        task_navigation_url=snapshot.task_navigation_url)
    navigation = {"tool": "browser_navigate", "args": {"target": "task"}}
    with pytest.raises(ValueError):
        host.invoke("browser_snapshot", {"flow": [navigation, {"tool": "browser_click", "args": {"ref": "mock"}}]})
    assert not calls
    result = decode_json_object(host.invoke("browser_snapshot", {"flow": [navigation, {"tool": "browser_read", "args": {}}]}))
    assert not result["ok"]
    assert len(calls) == 1 and calls[0].args == {"url": url}


def test_flow_snapshot_binds_subsequent_readable_actions_to_its_own_receipt() -> None:
    session = session_with_snapshot(checks=())
    snapshot = offer_browser_tools(build_turn_snapshot(session), session)
    entries = tuple(replace(entry, output_schema={"type": "object"}) if entry.name == "browser_snapshot" else entry
                    for entry in snapshot_catalog(snapshot).entries)
    value = decode_json_object(session._memory_results["snapshot"].canonical["value"])
    value["scope"] = {"root_ref": "", "selector": ""}
    calls: list[ToolCall] = []

    def execute(call: ToolCall) -> ToolResult:
        calls.append(call)
        if call.name == "browser_snapshot":
            return ToolResult(call, "controls", ok=True, canonical={"value": value},
                              audit={"result_ref": "flow-observation", "browser_document": DOCUMENT})
        return ToolResult(call, "action unavailable", ok=False)

    host = CodeModeHost(execution_id="flow", catalog=entries, execute=execute, allow_writes=True)
    assert decode_json_object(host.invoke("browser_snapshot", {"flow": [{"tool": "browser_snapshot", "args": {}}]}))["ok"]
    assert not decode_json_object(host.invoke("browser_click", {"ref": {"role": "button", "name": "Details"}}))["ok"]
    assert calls[-1].args == {"ref": "current-one"}
    assert host._control_snapshot_ref == "flow-observation"


def test_calculation_references_both_original_operands() -> None:
    ledger = ResearchLedger()
    for label, text in (("p1", "The measured input is 31 kg."), ("p2", "The measured input is 24 kg.")):
        url = "https://example.org/" + label
        ledger.read_passages[label] = ReadPassage(label, OpenedSource(url, url, "", "", label), text, (), 0, len(text))
    scope = AnswerScope(digest("task"), (("e1", "p1"), ("e2", "p2")), ("attempt",))
    markdown = "The difference is **7 kg** (31 kg - 24 kg) [e1][e2]."
    args: ToolArgs = {"state": "answered", "markdown": markdown}
    evidence = check_report_evidence(submit_answer(scope, args), ledger.read_passages)
    rows = evidence["verified_calculations"]
    assert isinstance(rows, list) and len(rows) == 1
    row = decode_json_object(rows[0])
    assert row["result"] == "7" and row["unit"] == "kg"
    assert "p1" in str(row["left_sources"]) and "p2" in str(row["right_sources"])
    with pytest.raises(ValueError, match="calculation_mismatch"):
        check_report_evidence(submit_answer(scope, {**args, "markdown": markdown.replace("7 kg", "8 kg")}), ledger.read_passages)


@pytest.mark.parametrize("expression", ["55 kg (31 kg + 24 kg)", "-7 kg (24 kg - 31 kg)", "7 kg (31 kg − 24 kg)"])
def test_arithmetic_keeps_entity_and_condition_review(expression: str) -> None:
    ledger = ResearchLedger()
    for label, text in (("p1", "Cedar used 31 kg of fuel during winter."), ("p2", "Birch used 24 kg of fuel during summer.")):
        url = "https://example.org/" + label
        ledger.read_passages[label] = ReadPassage(label, OpenedSource(url, url, "", "", label), text, (), 0, len(text))
    scope = AnswerScope(digest("task"), (("e1", "p1"), ("e2", "p2")), ("attempt",))
    check = check_report_evidence(submit_answer(scope, {"state": "answered",
        "markdown": f"Unrelated entities under identical winter conditions yield {expression} [e1][e2]."}), ledger.read_passages)
    assert check["verified_calculations"]
    assert check["needs_review"], "Arithmetic must not suppress source context checks for its operands"


def test_legacy_submission_diagnostics_survive_signed_calculation_support() -> None:
    url = "https://example.org/original"
    text = "The measured amount is 7 kg."
    passage = ReadPassage("p1", OpenedSource(url, url, "", "", "original"), text, (), 0, len(text))
    scope = AnswerScope(digest("task"), (("e1", "p1"),), ("attempt",))
    submission = submit_answer(scope, {"state": "answered", "markdown": "The claimed amount is -8 kg [e1]."})
    old = check_report_evidence(submission, {"p1": passage}, verify_calculations=False)
    current = check_report_evidence(submission, {"p1": passage})
    assert old == {"verified_quotes": [], "shared_source_groups": [],
                   "needs_review": [{"kind": "quantity_needs_context", "quantity": "8 kg", "labels": ["e1"]}]}
    reviews = current["needs_review"]
    assert isinstance(reviews, list) and decode_json_object(reviews[0])["quantity"] == "-8 kg"


def test_conflict_search_keeps_independent_opposition_in_every_order() -> None:
    ledger = ResearchLedger()
    calls: list[ToolCall] = []
    for ref, origin, direction in (
        ("a", "first", "lower"),
        ("b", "second", "higher"),
        ("c", "first", "higher"),
    ):
        url = f"https://{origin}.example/paper"
        source = OpenedSource(url, url, "", "", ref)
        ledger.read_passages[ref] = ReadPassage(
            ref, source, direction, (), 0, len(direction)
        )
        calls.append(ToolCall("save_finding", {
            "question_ref": "q1",
            "passage_ref": ref,
            "conditions": [],
            "comparison": {
                "subject": "Atlas", "factor": "A", "comparator": "B",
                "endpoint": "fuel", "direction": direction,
            },
        }))

    for ordered in permutations(calls):
        found = conflict_search(ledger, remaining_searches=1, findings=ordered)
        assert found is not None, [call.args["passage_ref"] for call in ordered]
        refs = found["passage_refs"]
        assert isinstance(refs, list) and "a" in refs and "b" in refs

    assert conflict_search(ledger, remaining_searches=1, findings=(calls[0], calls[2])) is None


def test_conflict_search_requires_a_passage_and_a_remaining_slot() -> None:
    ledger = ResearchLedger()
    ledger.questions = ("Atlas fuel consumption at the documented 10 L workload",)
    claim = ledger.questions[0]
    for number, stance in ((1, "supports"), (2, "contradicts")):
        ref = f"p{number}"
        url = "https://example.org/" + ref
        ledger.read_passages[ref] = ReadPassage(ref, OpenedSource(url, url, "", "", ref), claim, (), 0, len(claim))
        ledger.references[f"f{number}"] = ResearchReference(f"observation-{number}", 1, "claim", "current_run", ref, ref, "q1")
        ledger.evidence_items.append(EvidenceItem(claim, url, claim, stance, ref))
    assert conflict_search(ledger, remaining_searches=0) is None
    selected = conflict_search(ledger, remaining_searches=1)
    assert selected is not None and selected["passage_refs"] == ["p1", "p2"]
    ledger.investigations["a1"] = {"tool": "web_search", "ok": False, "question_ref": "q1",
                                    "args": {"query": selected["query"]}, "passage_refs": []}
    assert conflict_search(ledger, remaining_searches=1) is None


@pytest.mark.usefixtures("public_source_dns", "scripted_local_api_connection", "no_external_advisor_models")
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("immediate_submission", [False, True])
def test_production_supplements_opposing_comparisons_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, native: bool, immediate_submission: bool,
) -> None:
    directory = tmp_path / "research"
    if native:
        monkeypatch.setattr(gate, "ExperimentProvider", NativeExperimentProvider)

    def search(provider: object, query: str, *, limit: int) -> list[JsonObject]:
        return [{"title": "Cedar benchmark", "url": CEDAR, "snippet": "Atlas fuel consumption benchmark"}]

    monkeypatch.setattr(ResumableBrowserSearchProvider, "search", search)

    def replies() -> Iterator[JsonObject]:
        yield {"tool": "open_url", "args": {"target": {"url": BIRCH}}}
        for url, direction, fuel in ((CEDAR, "lower", 24), (BIRCH, "higher", 31)):
            receipts = tool_receipts(directory / "state")
            refs = {row.result.call.args.get("url"): read_passage_refs(row.result.canonical["read_passages"])[0]
                    for row in receipts if row.result.call.name == "open_url" and row.result.ok and row.result.canonical.get("read_passages")}
            yield {"tool": "save_finding", "args": {"passage_ref": refs[url],
                "claim": f"Atlas method A consumed {fuel} kg under the fixed workload.",
                "comparison": {"subject": "Atlas", "factor": "method A", "comparator": "method B",
                    "endpoint": "fuel consumption", "direction": direction, "uncertainty": "unknown"}}}
        if not immediate_submission:
            yield {"tool": "source_search", "args": {"target": "task", "query": "benchmark"}}
            yield {"tool": "source_search", "args": {"target": {"url": BIRCH}, "query": "benchmark"}}
        yield {"tool": "submit_research_answer", "args": {"state": "answered",
            "markdown": "The recorded amounts are 31 kg and 24 kg. The difference is 7 kg (31 kg - 24 kg) [e1][e2]."}}

    install_http(monkeypatch, replies(), native=native)
    report = gate.run_case(directory, CASE, Target("http://scripted.test/v1", "fixture", 123456789, 10))
    assert report["passed"] is True, report
    results = [row.result for row in tool_receipts(directory / "state")]
    assert sum(result.call.name == "save_finding" and result.ok for result in results) == 2
    searches = [result for result in results if result.call.name == "web_search"]
    assert len(searches) == 1 and searches[0].ok
    submission = next(result for result in results if result.call.name == "submit_research_answer" and result.ok)
    assert decode_json_object(submission.canonical["research_evidence_check"])["verified_calculations"]
