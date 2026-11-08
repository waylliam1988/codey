"""Unavailable websites close honestly using actual failed source receipts."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codey.operations.completion_gate import evaluate
from codey.operations.task_session import TaskSession
from codey.policies.task_policy import TaskPolicy
from codey.providers.base import result_metadata_from_text
from codey.runtime.core.models import ToolCall, ToolResult
from tests.test_explicit_web_entry_and_homepage import PageProvider

URL = "https://example.com/"
REF = "task_turn_effect:source_failure"


def source_session(*, actual_failure: bool = True) -> TaskSession:
    session = TaskSession(TaskPolicy(frozenset({"web.read", "control"}), sources_open_required=True,
                                    required_checks=("research_sources_opened",)),
                          task_kind="chat", task_text=f"请打开 {URL} 看看是干嘛的",
                          required_source_urls=frozenset({URL}))
    session._memory_results[REF] = ToolResult(ToolCall("open_url", {"url": URL}), "ERROR: page_blank", ok=False,
        canonical={"source_failure": {"url": URL, "detail": "page_blank"}} if actual_failure else {})
    session.last_done_args = {"source_gaps": [{"result_ref": REF, "reason": "page_blank"}]}
    return session


def test_actual_unavailable_source_is_blocked_without_verified_completion() -> None:
    verdict = evaluate(source_session(), "没有读取到页面，无法验证网站内容")
    assert verdict.blocked and not verdict.complete and verdict.proof is None


def test_fresh_work_view_exposes_verified_failure_and_closure_args() -> None:
    from codey.operations.task_guidance import task_working_context

    context = task_working_context(source_session())
    marker = "Requested source coverage (verified runtime observations):\n"
    payload, _ = json.JSONDecoder().raw_decode(context.split(marker, 1)[1])
    assert payload["failed_sources"] == [{"url": URL, "result_ref": REF, "detail": "page_blank"}]
    assert payload["unattempted_urls"] == []
    assert '"unattempted_urls": ["https://example.com/"]' in task_working_context(source_session(actual_failure=False))
    session = source_session()
    session.task_kind = "research"
    assert marker not in task_working_context(session)


def test_fresh_chat_work_view_keeps_successful_source_progress() -> None:
    from codey.operations.task_guidance import task_working_context

    session = source_session()
    session.opened_sources.add(URL)
    session.source_ids["s1"] = URL
    context = task_working_context(session)
    payload, _ = json.JSONDecoder().raw_decode(context.split("Opened external sources (verified runtime observations):\n", 1)[1])
    assert payload == {"opened_sources": [URL], "source_ids": {"s1": URL}}
    assert "Unavailable requested sources" not in context


def test_model_cannot_turn_a_call_validation_error_into_an_acquisition_attempt() -> None:
    verdict = evaluate(source_session(actual_failure=False), "无法验证")
    assert not verdict.blocked and not verdict.complete
    assert "failed acquisition" in verdict.followup


def test_unknown_reference_or_unaccounted_requested_url_cannot_close() -> None:
    session = source_session()
    session.last_done_args = {"source_gaps": [{"result_ref": "invented", "reason": "no access"}]}
    assert not evaluate(session, "unavailable").blocked
    session = source_session()
    session.task_text += "，再打开 https://other.example.com/"
    session.required_source_urls |= {"https://other.example.com/"}
    assert not evaluate(session, "unavailable").blocked


def test_strict_research_keeps_its_existing_evidence_contract() -> None:
    session = source_session()
    session.task_kind = "research"
    verdict = evaluate(session, "cannot read")
    assert not verdict.complete and not verdict.blocked


@pytest.mark.parametrize("failure", ["policy", "offset", "fetch"])
def test_only_actual_fetch_failure_can_be_used_for_source_gaps(monkeypatch, failure: str) -> None:
    from codey.operations.task_execution import ExecutionDelegate
    from codey.research.ledger import ResearchLedger
    from codey.research.tools import ResearchTools

    class Source:
        def search(self, query: str, *, limit: int) -> list[dict[str, object]]:
            return []

        def fetch(self, url: str) -> dict[str, object]:
            if failure == "fetch":
                return {"status": "error", "url": url, "detail": "HTTP 403"}
            return {"status": "ok", "url": url, "title": "Article", "text": "readable article text"}

    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url",
                        lambda *a, **kw: "network_policy_denied" if failure == "policy" else None)
    url = "https://example.com/article"
    tools = ResearchTools(Source(), None, None)
    opened = tools.open_url(url, offset=1000 if failure == "offset" else 0)
    assert not opened.ok
    delegate = ExecutionDelegate(session=source_session(), research_tools=tools)
    result, _, _ = delegate._opened_result(ToolCall("open_url", {"url": url}), opened, url,
                                           ledger=ResearchLedger(), turn=1, tool_index=0)
    assert ("source_failure" in result.canonical) == (failure == "fetch")


class FailureProvider(PageProvider):
    sent = False

    def send(self, text: str, timeout: float | None = None) -> str:
        if not self.sent:
            self.sent = True
            return json.dumps({"tool": "open_url", "args": {"url": URL}})
        metadata = result_metadata_from_text(text)
        assert metadata is not None
        return json.dumps({"tool": "done", "args": {"summary": "页面为空，无法验证网站内容。",
            "source_gaps": [{"result_ref": metadata.result_ref, "reason": "page_blank"}]}})


@pytest.mark.usefixtures("scripted_local_api_connection", "no_external_advisor_models", "public_source_dns")
def test_real_entry_delivers_failed_page_answer_without_repeating_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from codey.app.headless_runner import HeadlessRequest, run_headless

    calls: list[str] = []

    def fetch(url: str) -> dict[str, object]:
        calls.append(url)
        return {"status": "error", "url": url, "detail": "HTTP fallback had no usable visible content: page_blank"}

    monkeypatch.setenv("NATIVE_TOOLS", "0")
    monkeypatch.setattr("codey.research.source_download._download_text_fallback", fetch)
    provider = FailureProvider()
    events: list[dict[str, object]] = []
    result = run_headless(HeadlessRequest(None, f"打开 {URL} 看看这个网站是做什么的", intent="chat",
        provider_id="local", max_turns=4, state_home=tmp_path), emit_jsonl=events.append,
        connect_provider=lambda *args, **kwargs: provider)
    assert result.stop_reason == "blocked" and calls == [URL], events[-1].get("summary")
    terminal = next(row for row in events if row.get("type") == "task_done")
    assert terminal["summary"] == "页面为空，无法验证网站内容。"
