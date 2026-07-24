from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.store import KnowledgeStore
from tests.manual import deep_research_core_ab as ab


def test_deep_research_ab_prompt_arms_are_isolated() -> None:
    baseline = ab.ProbeJsonToolCodec("baseline").system_prompt()
    source_search = ab.ProbeJsonToolCodec("source_search").system_prompt()
    deep_core = ab.ProbeJsonToolCodec("deep_core").system_prompt()

    assert "Probe fixture discipline" in baseline
    assert "built-in web search" in baseline
    assert "source_search" not in baseline
    assert "Probe fixture discipline" in source_search
    assert "source_search" in source_search
    assert "Deep Research Core experimental guidance" not in source_search
    assert "Deep Research Core experimental guidance" in deep_core

    plan = ab.ProbeJsonToolCodec("deep_core").parse(
        '{"tool":"source_search","args":{"url":"https://example.edu/omega-method.pdf","query":"bootstrap"}}'
    )

    assert plan.calls
    assert plan.calls[0].name == "source_search"


def test_deep_research_ab_profile_defaults_keep_live_probe_small() -> None:
    cheap_cases = ab._selected_cases([], profile="cheap")
    full_cases = ab._selected_cases([], profile="full")

    assert tuple(case.name for case in cheap_cases) == ab.CHEAP_CASE_NAMES
    assert len(cheap_cases) < len(full_cases)
    assert ab._selected_arms([], profile="cheap") == ab.CHEAP_ARMS
    assert ab._selected_arms([], profile="full") == ab.ARMS
    assert ab._profile_max_turns("cheap") == ab.CHEAP_MAX_TURNS
    assert ab._profile_max_turns("full") == ab.FULL_MAX_TURNS
    assert ab._profile_max_turns("cheap", 3) == 3


def test_deep_research_ab_live_trace_writes_reply_json_immediately() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "trace.json"
        trace = ab.LiveTrace(path)

        trace.record_reply(
            provider="deepseek",
            case="pdf-target-page",
            arm="source_search",
            send_index=1,
            message="prompt",
            reply='{"tool":"web_search","args":{"query":"omega"}}',
        )

        payload = json.loads(path.read_text(encoding="utf-8"))
        temp_files = list(Path(td).glob("*.tmp"))

    assert payload["probe"] == "deep_research_core_ab_trace"
    assert payload["event_count"] == 1
    event = payload["events"][0]
    assert event["event"] == "reply"
    assert event["provider"] == "deepseek"
    assert event["reply"] == '{"tool":"web_search","args":{"query":"omega"}}'
    assert not temp_files


def test_deep_research_ab_probe_rejects_tool_call_flood() -> None:
    reply = "\n".join(
        json.dumps({"tool": "web_search", "args": {"query": f"alpha {index}"}})
        for index in range(ab.MAX_CALLS_PER_TURN + 2)
    )

    plan = ab.ProbeJsonToolCodec("baseline").parse(reply)

    assert not plan.calls
    assert plan.control is None
    assert "too many JSON tool calls" in (plan.protocol_error or "")


def test_deep_research_ab_probe_rejects_two_duplicate_tool_calls() -> None:
    reply = (
        'json{"tool":"knowledge_search","args":{"query":"alpha"}}'
        'json{"tool":"knowledge_search","args":{"query":"alpha"}}'
    )

    plan = ab.ProbeJsonToolCodec("baseline").parse(reply)

    assert not plan.calls
    assert plan.control is None
    assert "too many JSON tool calls" in (plan.protocol_error or "")


def test_deep_research_ab_probe_rejects_tool_call_plus_done() -> None:
    reply = (
        '{"tool":"knowledge_write","args":{"type":"source","title":"Alpha"}}\n'
        '{"tool":"done","args":{"answer":"report"}}'
    )

    plan = ab.ProbeJsonToolCodec("baseline").parse(reply)

    assert not plan.calls
    assert plan.control is None
    assert "too many JSON tool calls" in (plan.protocol_error or "")


def test_deep_research_ab_probe_repeats_question_in_followups() -> None:
    question = "Does the Alpha Safety Program require 72-hour incident notification?"
    codec = ab.ProbeJsonToolCodec("baseline", question=question)

    assert question in codec.repair_prompt()
    assert question in codec.format_results([])
    assert "do not use ASCII double-quote search operators" in codec.repair_prompt()



def test_deep_research_ab_probe_extracts_malformed_done_answer() -> None:
    reply = (
        '{"tool":"done","args":{"answer":"## 结论\\n'
        '搜索覆盖里写了："Alpha Safety Program query" — 返回结果\\n'
        '## 来源\\n[1] Title - https://example.com"}}'
    )

    plan = ab.ProbeJsonToolCodec("baseline").parse(reply)

    assert not plan.calls
    assert plan.control is not None
    assert plan.control.kind == "done"
    assert "Alpha Safety Program query" in plan.control.body


def test_source_search_requires_opened_source_and_returns_pdf_page_locator() -> None:
    case = next(item for item in ab.CASES if item.name == "pdf-target-page")

    with tempfile.TemporaryDirectory() as td:
        store = KnowledgeStore(Path(td))
        tools = ab.ProbeResearchTools(
            ab.FixtureSearchProvider(case),
            store,
            KnowledgeChanges(store.root),
        )

        before = tools.source_search(ab.PDF_METHOD_URL, "stratified bootstrap")
        opened = tools.open_url(ab.PDF_METHOD_URL)
        located = tools.source_search(ab.PDF_METHOD_URL, "stratified bootstrap")
        page = tools.open_url(ab.PDF_METHOD_URL, pages="9")
        evidence_count = len(tools.ledger.evidence_items)
        store.close()

    assert before.startswith("NEEDS_OPEN:")
    assert "[page 1]" in opened
    assert "p.9" in located
    assert "stratified bootstrap validation" in located
    assert "[page 9]" in page
    assert evidence_count == 0


def test_deep_research_ab_scoring_tracks_source_search_recall() -> None:
    case = next(item for item in ab.CASES if item.name == "pdf-target-page")
    report = (
        "## Conclusion\n"
        "- The Omega method uses stratified bootstrap validation [1 p.9].\n\n"
        "## Key evidence\n"
        "- [1 p.9] The method uses stratified bootstrap validation.\n\n"
        "## Counter-evidence\n"
        "- No strong counter-evidence was found; this fixture only checks source_search.\n\n"
        "## Source quality\n"
        "- [1] primary paper source.\n\n"
        "## Search coverage\n"
        "- query: omega method validation\n\n"
        "## Sources\n"
        f"[1] Omega method paper - {ab.PDF_METHOD_URL}"
    )
    provider = ab.ScriptedProvider(
        json.dumps({"tool": "open_url", "args": {"url": ab.PDF_METHOD_URL}}),
        json.dumps({
            "tool": "source_search",
            "args": {"url": ab.PDF_METHOD_URL, "query": "stratified bootstrap"},
        }),
        json.dumps({"tool": "open_url", "args": {"url": ab.PDF_METHOD_URL, "pages": "9"}}),
        json.dumps({
            "tool": "knowledge_write",
            "args": {
                "type": "fact",
                "title": "Omega validation method",
                "body": "The method uses stratified bootstrap validation.",
                "sources": [ab.PDF_METHOD_URL],
                "evidence": [{
                    "claim": "Omega method uses stratified bootstrap validation.",
                    "source_url": ab.PDF_METHOD_URL,
                    "excerpt": "stratified bootstrap validation with 5,000 resamples",
                    "page": 9,
                }],
            },
        }),
        json.dumps({"tool": "done", "args": {"answer": report}}),
    )

    row = ab.run_case(
        provider,
        "scripted",
        case,
        "source_search",
        max_turns=8,
        timeout=30.0,
    )

    assert row["final_done"]
    assert row["used_source_search"]
    assert row["send_count"] == 5
    assert row["reply_count"] == 5
    assert row["done_attempts"] == 1
    assert row["protocol_repair_prompts"] == 0
    assert row["quality_repair_prompts"] == 0
    assert row["raw_reply_previews"]
    assert "done" in row["raw_reply_previews"][-1]
    assert row["last_done_quality_review"]["ok"]
    assert row["opened_target_page_or_offset"]
    assert row["target_fact_reported"]
    assert row["saved_exact_evidence_snippet"]
    assert row["quality_score"] > 0


def test_deep_research_ab_summary_reports_deltas() -> None:
    rows = [
        {
            "arm": "baseline",
            "quality_score": 2,
            "final_done": True,
            "primary_applicable": True,
            "opened_primary_source": False,
            "used_source_search": False,
            "target_locator_applicable": True,
            "opened_target_page_or_offset": False,
            "target_report_applicable": True,
            "target_fact_reported": False,
            "target_evidence_applicable": True,
            "saved_exact_evidence_snippet": False,
            "counter_applicable": False,
            "reported_counter_or_limits": True,
            "local_memory_applicable": False,
            "used_local_memory": True,
            "max_turns_failure": False,
            "unsupported_citation_count": 0,
            "turns_used": 4,
        },
        {
            "arm": "source_search",
            "quality_score": 6,
            "final_done": True,
            "primary_applicable": True,
            "opened_primary_source": True,
            "used_source_search": True,
            "target_locator_applicable": True,
            "opened_target_page_or_offset": True,
            "target_report_applicable": True,
            "target_fact_reported": True,
            "target_evidence_applicable": True,
            "saved_exact_evidence_snippet": True,
            "counter_applicable": False,
            "reported_counter_or_limits": True,
            "local_memory_applicable": False,
            "used_local_memory": True,
            "max_turns_failure": False,
            "unsupported_citation_count": 0,
            "turns_used": 5,
        },
    ]

    summary = ab._summarize(rows)

    assert summary["arms"]["baseline"]["count"] == 1
    assert summary["arms"]["source_search"]["used_source_search_rate"] == 1.0
    assert summary["source_search_delta_vs_baseline"]["avg_quality_score"] == 4.0


def test_deep_research_ab_open_if_missing_controls_provider_launch() -> None:
    with mock.patch.object(ab, "connect_provider", side_effect=RuntimeError("offline")) as connect:
        ab.run_provider(
            "qwen",
            port=9222,
            open_if_missing=True,
            arms=("baseline",),
            cases=(ab.CASES[0],),
            max_turns=1,
            timeout=1.0,
            no_new_chat=False,
            new_chat_timeout=1.0,
        )

    connect.assert_called_once_with(
        "qwen",
        port=9222,
        open_if_missing=True,
        bring_to_front=True,
    )


def test_deep_research_ab_error_rows_include_provider_failure() -> None:
    failure = mock.Mock()
    failure.to_dict.return_value = {
        "model": "Qwen Studio",
        "action": "new_chat",
        "url": "https://chat.qwen.ai/",
        "title": "Qwen",
        "message": "timed out",
        "time": "2026-07-24T00:00:00+00:00",
        "kind": "transient",
        "stage": "new_chat",
    }
    provider = mock.Mock()
    provider.last_failure = failure
    provider.new_chat.side_effect = RuntimeError("new chat failed")

    with mock.patch.object(ab, "connect_provider", return_value=provider):
        result = ab.run_provider(
            "qwen",
            port=9222,
            open_if_missing=False,
            arms=("baseline",),
            cases=(ab.CASES[0],),
            max_turns=1,
            timeout=1.0,
            no_new_chat=False,
            new_chat_timeout=1.0,
        )

    row = result["rows"][0]
    assert row["error"] == "RuntimeError: new chat failed"
    assert row["provider_failure"]["action"] == "new_chat"
    assert row["provider_failure"]["message"] == "timed out"
