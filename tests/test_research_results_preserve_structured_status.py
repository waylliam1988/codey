"""Source and knowledge results retain status through rendering and receipts."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from codey.research.tools import ResearchTools


def test_search_empty_success_and_error_are_distinct_structured_results():
    tools = ResearchTools(search=None, store=None, changes=None)
    for error, expected in [("", True), ("unavailable", False)]:
        gateway = SimpleNamespace(search=lambda *_, error=error: SimpleNamespace(error=error, hits=[]))
        with mock.patch.object(ResearchTools, "gateway", new_callable=mock.PropertyMock, return_value=gateway):
            result = tools.web_search("query")
        assert result.ok is expected


def test_source_needs_open_is_unsuccessful_without_parsing_its_message():
    tools = ResearchTools(search=None, store=None, changes=None)
    gateway = SimpleNamespace(search_inside=lambda *_: SimpleNamespace(status="needs_open", detail="请先打开来源"))
    with mock.patch.object(ResearchTools, "gateway", new_callable=mock.PropertyMock, return_value=gateway):
        result = tools.source_search("https://example.com", "query")
    assert result.ok is False
    assert "请先打开来源" in result.model_text


def test_output_receipt_does_not_treat_successful_error_example_as_failure():
    from codey.research.output_receipts import maybe_externalize_output
    from codey.runtime.core.models import ToolCall

    result = maybe_externalize_output(
        store=None, session_id="s", run_id="r", permission_profile="research", call=ToolCall("open_url", {}),
        output="ERROR: an example inside the source", ok=True, turn=1, tool_index=0,
    )
    assert result.ok is True
    assert result.model_text == "ERROR: an example inside the source"


def test_knowledge_link_reports_failure_as_data(tmp_path):
    from codey.knowledge.store import KnowledgeStore

    store = KnowledgeStore(tmp_path)
    try:
        outcome = store.link("missing", "also-missing")
        assert outcome.ok is False
        assert outcome.created is False
    finally:
        store.index.close()
