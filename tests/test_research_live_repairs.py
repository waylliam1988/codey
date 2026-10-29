"""Regressions captured during real local-model browser research."""

from types import SimpleNamespace
from unittest import mock

import pytest

from codey.knowledge.changes import KnowledgeChanges
from codey.knowledge.model import KnowledgeObject
from codey.knowledge.store import KnowledgeStore
from codey.research import browser_search
from codey.research.ledger import ResearchLedger
from codey.research.source_gateway import ResearchSourceGateway
from codey.research.tools import ResearchTools

URL = "https://example.com/article"


@pytest.mark.usefixtures("public_source_dns")
def test_chinese_challenge_is_not_acquired_as_citable_evidence():
    response = mock.MagicMock(status=200, headers={"content-type": "text/html; charset=utf-8"})
    response.__enter__.return_value = response
    response.geturl.return_value = URL
    response.read.return_value = (
        "<html><title>页面安全校验中</title><body>页面安全校验中。"
        "正在验证您的浏览器，请稍候。校验通过后将自动跳转到请求的页面。</body></html>"
    ).encode()
    ledger = ResearchLedger()
    gateway = ResearchSourceGateway(SimpleNamespace(fetch=browser_search._download_text_fallback), ledger)
    with mock.patch.object(browser_search, "_open_url_no_redirect", return_value=response):
        result = gateway.open(URL)
    assert result.status == "error" and "page_unavailable" in result.detail
    assert not ledger.opened_sources and not ledger.final_url_set()


def test_long_article_about_browser_verification_is_still_usable():
    text = "研究浏览器人机验证的可用性与误判。" + "实验方法、对照和观测结果。" * 180
    assert browser_search._usable_fetch_page_text(text)


def test_missing_sources_feedback_repairs_omission_without_reopening_or_relabelling(tmp_path):
    store = KnowledgeStore(tmp_path)
    try:
        tools = ResearchTools(None, store, KnowledgeChanges(root=store.root))
        tools.sources_read.add(URL)
        tools.ledger.record_open(URL, URL, "Experiment", "Only one writer commits at a time.")
        args = {"type": "fact", "title": "Single writer", "body": "Only one writer commits at a time."}
        rejected = tools.knowledge_write(args)
        assert not rejected.ok
        assert f"Already opened URLs: {URL}" in rejected.model_text
        assert "sources=[opened URL]" in rejected.model_text
        assert "or use type 'hypothesis'" not in rejected.model_text
        repaired = tools.knowledge_write({**args, "sources": [URL], "evidence": [{
            "claim": args["body"], "source_url": URL, "excerpt": args["body"], "stance": "supports",
        }]})
        assert repaired.ok and len(tools.ledger.evidence_items) == 1
    finally:
        store.close()


@pytest.mark.parametrize("evidence", [None, []])
def test_omitted_evidence_does_not_promote_page_navigation_to_support(tmp_path, evidence):
    store = KnowledgeStore(tmp_path)
    try:
        tools = ResearchTools(None, store, KnowledgeChanges(root=store.root))
        tools.sources_read.add(URL)
        tools.ledger.record_open(URL, URL, "Database guide", "DB Home Docs Search. Only one writer at a time.")
        args = {"type": "fact", "title": "Database behavior", "body": "Readers never block a writer.",
                "sources": [URL], "evidence": evidence}
        rejected = tools.knowledge_write(args)
        assert not rejected.ok and "require explicit evidence" in rejected.model_text
        assert not tools.ledger.evidence_items and store.index.count() == 0
        # Cataloging a source remains possible but does not manufacture a claim.
        catalog = tools.knowledge_write({**args, "type": "source"})
        assert catalog.ok and not tools.ledger.evidence_items
        assert store.index._read_conn().execute("SELECT COUNT(*) FROM knowledge_versions WHERE kind='claim'").fetchone()[0] == 0
    finally:
        store.close()


@pytest.mark.parametrize("conditions, path", [
    ({"name": "WAL", "state": "reported", "value": "active"}, "conditions.name"),
    ({"mode": {"state": "unknown"}}, "conditions.mode.state"),
    ({"mode": {"state": [], "value": "WAL"}}, "conditions.mode.state"),
    ({"mode": {"state": "reported", "value": 5}}, "conditions.mode"),
    ({"mode": {"state": "not_extracted", "name": "workload"}}, "conditions.mode"),
])
def test_condition_errors_identify_path_and_show_nested_repair(conditions, path):
    with pytest.raises(ValueError) as caught:
        KnowledgeObject("claim-one", "claim", "A conditional claim", "source", conditions=conditions)
    assert path in str(caught.value) and '"workload":{"state":"not_extracted"}' in str(caught.value)


def test_verified_normalized_quote_keeps_original_source_offsets():
    from codey.research.object_model import _excerpt_offsets, build_research_record

    text = "Document header.\nOnly one\n  writer may commit at a time.\nReferences."
    excerpt = "only one writer may commit at a time."
    ledger = ResearchLedger()
    ledger.record_open(URL, URL, "Concurrency", text)
    prepared = ledger.prepare_evidence_items([{"source_url": URL, "claim": "Single writer", "excerpt": excerpt}],
        fallback_sources=[URL], fallback_claim="Single writer", fallback_body="", note_type="fact")
    assert not prepared.error and len(prepared.items) == 1
    ledger.add_evidence_items(list(prepared.items))
    record = build_research_record(question="Concurrent writers?", summary="", ledger=ledger)
    locator = record.evidence[0].locator
    assert text[locator.char_start:locator.char_end] == "Only one\n  writer may commit at a time."
    assert _excerpt_offsets(text, "one ... writer") == (0, 0)
    assert _excerpt_offsets(text, "a different claim") == (0, 0)


@pytest.mark.parametrize("case", ["valid", "unopened", "invented_quote", "explicit_empty_sources"])
def test_new_note_can_catalogue_explicit_evidence_urls_without_weakening_checks(tmp_path, case):
    store = KnowledgeStore(tmp_path)
    try:
        tools = ResearchTools(None, store, KnowledgeChanges(root=store.root))
        quote = "Only one writer commits at a time."
        tools.sources_read.add(URL)
        tools.ledger.record_open(URL, URL, "Experiment", quote)
        args = {"type": "fact", "title": "Single writer", "body": quote, "evidence": [{
            "claim": quote, "source_url": URL, "excerpt": quote, "stance": "supports",
        }]}
        if case == "unopened":
            args["evidence"][0]["source_url"] = "https://example.com/unopened"
        elif case == "invented_quote":
            args["evidence"][0]["excerpt"] = "Many writers commit simultaneously."
        elif case == "explicit_empty_sources":
            args["sources"] = []
        result = tools.knowledge_write(args)
        assert result.ok == (case == "valid"), result.model_text
        assert len(tools.ledger.evidence_items) == (1 if case == "valid" else 0)
        if case == "valid":
            assert store.read_note(tools.created_ids[0]).sources == [URL]
        else:
            assert store.index.count() == 0
    finally:
        store.close()
