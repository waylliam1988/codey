from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from itertools import permutations
from pathlib import Path

import pytest

from codey.codemode.host import CodeModeHost
from codey.operations.browser.catalog import snapshot_catalog
from codey.operations.browser.turn_surface import offer_browser_tools
from codey.operations.task_session import TaskSession
from codey.operations.turn_snapshot import build_turn_snapshot
from codey.policies.task_policy import TaskPolicy
from codey.research.answer_contract import AnswerScope, digest, submit_answer
from codey.research.evidence_gaps import (
    comparison_gap_observations,
    conflict_search,
    same_source_groups,
    suspected_source_pairs,
)
from codey.research.ledger import EvidenceItem, ResearchLedger
from codey.research.passages import ReadPassage, read_passage_refs
from codey.research.reference import ResearchReference
from codey.research.report_evidence import check_report_evidence
from codey.research.search_contracts import SearchOutcome
from codey.research.source_document import OpenedSource
from codey.runtime.core.models import ToolCall, ToolResult
from codey.task.web_intent import source_url_key
from codey.types.json_value import JsonObject, ToolArgs, decode_json_object
from tests.support.search_fixture import search_outcome
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
                        task_navigation_url=snapshot.browser.navigation_url)
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


def test_conflict_search_keeps_reported_cohorts_after_literal_query_deduplication() -> None:
    ledger = ResearchLedger()
    ledger.questions = ("Compare Atlas fuel consumption between method A and method B.",)
    findings: list[ToolCall] = []
    for workload in (10, 100):
        for direction in ("lower", "higher"):
            ref = f"{workload}-{direction}"
            url = f"https://{direction}.example/{workload}"
            text = f"Atlas method A versus method B fuel consumption at {workload} L was {direction}."
            ledger.read_passages[ref] = ReadPassage(ref, OpenedSource(url, url, "", "", ref), text, (), 0, len(text))
            findings.append(ToolCall("save_finding", {"question_ref": "q1", "passage_ref": ref,
                "conditions": [{"name": "workload", "state": "reported", "value": f"{workload} L"}],
                "comparison": {"subject": "Atlas", "factor": "method A", "comparator": "method B",
                               "endpoint": "fuel consumption", "direction": direction}}))
    first = conflict_search(ledger, remaining_searches=2, findings=findings)
    assert first is not None and isinstance(query := first["query"], str)
    assert first == conflict_search(ledger, remaining_searches=2, findings=reversed(findings))
    assert "10 l" in query.lower()
    ledger.record_search(query, [])
    second = conflict_search(ledger, remaining_searches=1, findings=findings)
    assert second is not None and "100 l" in str(second["query"]).lower()
    assert first["comparison_group"] != second["comparison_group"]
    assert conflict_search(ledger, remaining_searches=0, findings=findings) is None
    before = comparison_gap_observations(ledger, findings)
    assert [row["query_attempted"] for row in before] == [True, False]
    assert [row["additional_passage_refs"] for row in before] == [[], []]
    url = "https://validation.example/combined"
    text = "Atlas method A versus method B fuel consumption was independently measured at 10 L and 100 L."
    ledger.read_passages["independent"] = ReadPassage(
        "independent", OpenedSource(url, url, "", "", "independent"), text, (), 0, len(text)
    )
    after = comparison_gap_observations(ledger, findings)
    assert [row["query_attempted"] for row in after] == [True, False]
    assert [row["additional_passage_refs"] for row in after] == [["independent"], ["independent"]]
    assert conflict_search(ledger, remaining_searches=1, findings=findings) is None


def test_near_duplicate_candidates_leave_source_identity_and_versions_intact() -> None:
    ledger = ResearchLedger()
    shared = (
        "實驗團隊先把所有容器留在同一個恆溫室，再依照隨機順序逐組測量標本，並記錄校準後的重量與溫度。"
        "每次測量間隔三分鐘，兩位操作員以預先設定的方法分別處理儀器與記錄，以便事後重現。"
        "結果以編碼取代產品名稱，並把未完成的觀察另外列出，避免計算平均值時遺漏資料。"
        "第三批標本在送達過程中有明顯溫度變化，因此研究者另行保存該批讀數與原始時間戳記。"
        "分析總表依照事先登記的程序呈現結果，相關圖表提供完整圖例與各欄位的單位。"
        "只有通過兩次校準的讀數進入主要分析。替代分析也列出相同範圍資料以便比較。"
        "場站兩次溫控紀錄有顯著差異，研究者補載場站識別碼以利後續核查。"
        "試驗管採用同批次封蓋並使用獨立標記。有任何未達規格的容器都留待另外分析。"
        "原始資料包含錯誤值處理過程，且保留測量者提供的關於實驗室環境變化的觀察。"
        "讀者可依附件查回每次實驗的紀錄。研究團隊保留全部表格，以免遺漏任何有效讀數。"
    )
    template = "欢迎访问本站，点击此处浏览最新公告，相关联系信息可查看当前网站页脚的统一帮助信息。"
    for ref, text in (("a", shared), ("b", shared + "資料整理者補充了一條簡短腳註。"),
                      ("c", (template + "\n") * 30 + "实验在海上进行，测量降水。"),
                      ("d", (template + "\n") * 30 + "研究记录山地风力变化。")):
        url = f"https://{ref}.example/article"
        ledger.read_passages[ref] = ReadPassage(ref, OpenedSource(url, url, "", "", ref), text, (), 0, len(text))
    aliases = tuple((ref, ref) for ref in ledger.read_passages)
    candidates = suspected_source_pairs(ledger.read_passages, aliases)
    assert [row["passage_refs"] for row in candidates.pairs] == [["a", "b"]]
    assert same_source_groups(ledger.read_passages, aliases) == []
    assert not candidates.limit_reached and candidates.compared == 1
    capped = suspected_source_pairs(ledger.read_passages, aliases, max_pairs=0)
    assert capped.limit_reached and not capped.pairs


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

    def search(provider: object, query: str, *, limit: int) -> SearchOutcome:
        return search_outcome(query, [{"title": "Cedar benchmark", "url": CEDAR, "snippet": "Atlas fuel consumption benchmark"}], limit=limit)

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
