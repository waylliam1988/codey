from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codey import browser_worker, cancellation
from codey.consensus import ConsensusAdvice
from codey.events import RunEvent, run_event_payload
from codey.knowledge import KnowledgeChanges, KnowledgeStore
from codey.models import ToolCall, ToolResult
from codey.research.advisors import EvidencePack, run_research_advisors
from codey.research.browser_search import BrowserSearchProvider
from codey.research.ledger import ResearchLedger
from codey.research.pdf_extract import PDF_MAX_BYTES, extract_pdf_document, parse_pages
from codey.research.provenance import provenance_problem
from codey.research.protocols import JsonToolCodec
from codey.research.report_quality import review_report_quality
from codey.research.runner import ResearchRunner
from codey.research.source_document import SourceDocument, SourcePage
from codey.research.tools import ResearchTools
from codey.research.url_policy import check_fetch_url


class FakeProvider:
    name = "Fake"
    location = "fake://provider"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.sent: list[str] = []
        self.new_chat_calls = 0

    def new_chat(self, timeout=None) -> None:
        self.new_chat_calls += 1

    def send(self, text: str, timeout=None) -> str:
        self.sent.append(text)
        if not self.replies:
            return json.dumps({"tool": "done", "args": {"answer": "done"}})
        return self.replies.pop(0)

    def close(self) -> None:
        pass


class FakeSearch:
    name = "fake-search"

    def search(self, query: str, limit: int = 8) -> list[dict]:
        return [{
            "title": "Helium article",
            "url": "https://example.com/helium",
            "snippet": "Helium supply.",
        }]

    def fetch(self, url: str) -> dict:
        return {
            "url": url,
            "title": "Helium article",
            "text": "Helium is separated from natural gas streams.",
            "truncated": False,
        }

    def close(self) -> None:
        pass


class RecordingSearch(FakeSearch):
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str, limit: int = 8) -> list[dict]:
        self.queries.append(query)
        return super().search(query, limit=limit)


class FakePdfPage:
    def __init__(self, text: str, stream_size: int = 0) -> None:
        self._text = text
        self._stream_size = stream_size

    def extract_text(self) -> str:
        return self._text

    def get_contents(self):
        if not self._stream_size:
            return []
        return [SimpleNamespace(get_data=lambda: b"x" * self._stream_size)]


class FakePdfReader:
    pages: list[FakePdfPage] = []

    def __init__(self, _stream) -> None:
        self.pages = list(type(self).pages)


def fake_pypdf(*page_texts: str):
    FakePdfReader.pages = [FakePdfPage(text) for text in page_texts]
    return mock.patch.dict(sys.modules, {"pypdf": SimpleNamespace(PdfReader=FakePdfReader)})


def valid_research_report(url: str = "https://example.com/helium", *, conclusion: str = "Helium supply depends on gas processing.") -> str:
    return (
        "## 结论\n"
        f"- {conclusion} [1]\n\n"
        "## 关键证据\n"
        "- [1] The opened source says helium is separated from natural gas streams.\n\n"
        "## 反证与限制\n"
        "- 未找到强反证；本轮搜索了 helium，并会被新的 primary supply data 推翻。\n\n"
        "## 来源质量\n"
        "- [1] secondary · web · undated · example.com\n\n"
        "## 搜索覆盖\n"
        "- query: helium\n"
        "- opened: Helium article\n"
        "- skipped: none representative\n"
        "- stop: enough for this narrow fixture\n\n"
        "## 来源\n"
        f"[1] Helium article - {url}"
    )


def helium_ledger(url: str = "https://example.com/helium") -> ResearchLedger:
    ledger = ResearchLedger()
    ledger.record_search("helium", [{
        "title": "Helium article",
        "url": url,
        "snippet": "Helium supply.",
    }])
    ledger.record_open(
        requested_url=url,
        final_url=url,
        title="Helium article",
        text="Helium is separated from natural gas streams. 2026 supply note.",
    )
    evidence = ledger.prepare_evidence_items(
        [{
            "claim": "Helium supply depends on gas processing.",
            "source_url": url,
            "excerpt": "Helium is separated from natural gas streams.",
            "stance": "supports",
        }],
        fallback_sources=[url],
        fallback_claim="Helium supply depends on gas processing.",
        fallback_body="Helium is separated from natural gas streams.",
        note_type="fact",
    )
    assert not evidence.error
    ledger.add_evidence_items(list(evidence.items), note_id="fact-1")
    return ledger


def pdf_ledger(url: str = "https://example.com/report.pdf") -> ResearchLedger:
    ledger = ResearchLedger()
    ledger.record_search("report pdf", [{
        "title": "Report PDF",
        "url": url,
        "snippet": "Report details.",
    }])
    ledger.record_open_document(SourceDocument(
        requested_url=url,
        final_url=url,
        title="Report PDF",
        content_kind="pdf",
        mime_type="application/pdf",
        text="[page 4]\nThe report states that PDF intake supports page-specific evidence.",
        page_count=12,
        pages_read=(4,),
        page_texts=(SourcePage(
            number=4,
            text="The report states that PDF intake supports page-specific evidence.",
        ),),
    ))
    evidence = ledger.prepare_evidence_items(
        [{
            "claim": "PDF intake supports page-specific evidence.",
            "source_url": url,
            "excerpt": "PDF intake supports page-specific evidence",
            "stance": "supports",
            "page": 4,
        }],
        fallback_sources=[url],
        fallback_claim="PDF intake supports page-specific evidence.",
        fallback_body="The report states that PDF intake supports page-specific evidence.",
        note_type="fact",
    )
    assert not evidence.error
    ledger.add_evidence_items(list(evidence.items), note_id="fact-pdf")
    return ledger


class FakeAdvisorProvider:
    name = "FakeAdvisor"

    def __init__(self, reply: str = "advisor note") -> None:
        self.reply = reply
        self.sent: list[str] = []
        self.new_chat_calls = 0
        self.closed = False

    def new_chat(self) -> None:
        self.new_chat_calls += 1

    def send(self, text: str, timeout=None) -> str:
        del timeout
        self.sent.append(text)
        return self.reply

    def close(self) -> None:
        self.closed = True


class SlowProvider(FakeProvider):
    thread_safe_send = True

    def __init__(
        self,
        delay: float,
        *,
        started: threading.Event | None = None,
        finished: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        super().__init__()
        self.delay = delay
        self.started = started
        self.finished = finished
        self.release = release

    def send(self, text: str, timeout=None) -> str:
        self.sent.append(text)
        if self.started is not None:
            self.started.set()
        try:
            if self.release is not None:
                self.release.wait(timeout=5.0)
            else:
                time.sleep(self.delay)
            return json.dumps({"tool": "done", "args": {"answer": "done"}})
        finally:
            if self.finished is not None:
                self.finished.set()


class ResearchBoundaryTests(unittest.TestCase):
    def test_research_package_exports_quality_gate_not_legacy_evidence_review(self) -> None:
        import codey.research as research

        self.assertTrue(callable(research.provenance_problem))
        self.assertTrue(callable(research.review_report_quality))
        self.assertFalse(hasattr(research, "review_final_summary"))
        self.assertFalse(hasattr(research, "EvidenceReviewResult"))
        self.assertFalse((Path(__file__).resolve().parents[1] / "codey" / "research" / "evidence_review.py").exists())

    def test_url_policy_rejects_private_targets_without_network(self) -> None:
        self.assertIn("local", check_fetch_url("http://localhost:8000", resolve=False))
        self.assertIn("non-public", check_fetch_url("http://127.0.0.1/", resolve=False))
        self.assertIsNone(check_fetch_url("https://example.com/page", resolve=False))

    def test_pdf_parse_pages_is_bounded_and_one_based(self) -> None:
        self.assertEqual(parse_pages("4"), (4,))
        self.assertEqual(parse_pages("1-12"), tuple(range(1, 11)))
        self.assertEqual(parse_pages("pages 1 - 5"), (1, 2, 3, 4, 5))
        self.assertEqual(parse_pages("pp.4-5"), (4, 5))
        self.assertEqual(parse_pages(""), (1, 2, 3, 4, 5))

    def test_pdf_extract_reads_default_pages_with_markers(self) -> None:
        with fake_pypdf(*(f"Page {index} text" for index in range(1, 8))):
            doc = extract_pdf_document(
                b"%PDF fixture",
                requested_url="https://example.com/report.pdf",
                final_url="https://example.com/report.pdf",
            )

        self.assertIsInstance(doc, SourceDocument)
        assert isinstance(doc, SourceDocument)
        self.assertEqual(doc.content_kind, "pdf")
        self.assertEqual(doc.page_count, 7)
        self.assertEqual(doc.pages_read, (1, 2, 3, 4, 5))
        self.assertIn("[page 1]", doc.text)
        self.assertIn("Page 5 text", doc.text)
        self.assertNotIn("Page 6 text", doc.text)

    def test_pdf_extract_skips_oversized_and_scanned_pdfs(self) -> None:
        oversized = extract_pdf_document(
            b"x" * (PDF_MAX_BYTES + 1),
            requested_url="https://example.com/report.pdf",
            final_url="https://example.com/report.pdf",
        )
        with fake_pypdf("", ""):
            scanned = extract_pdf_document(
                b"%PDF fixture",
                requested_url="https://example.com/report.pdf",
                final_url="https://example.com/report.pdf",
            )

        self.assertIn("too large", oversized.reason)
        self.assertIn("no extractable text", scanned.reason)

    def test_open_url_reads_pdf_page_selection_and_records_ledger(self) -> None:
        url = "https://example.com/report.pdf"

        class PdfSearch:
            def fetch(self, requested: str) -> dict:
                return {
                    "url": requested,
                    "title": "Report PDF",
                    "text": "",
                    "content_kind": "pdf",
                    "mime_type": "application/pdf",
                    "bytes": b"%PDF fixture",
                    "truncated": False,
                }

        with tempfile.TemporaryDirectory() as td, fake_pypdf(
            "page one",
            "page two",
            "page three",
            "The fourth page contains page-specific PDF evidence.",
            "page five",
            "page six",
        ):
            store = KnowledgeStore(Path(td))
            tools = ResearchTools(PdfSearch(), store, KnowledgeChanges(store.root))

            output = tools.open_url(url, pages="4")
            opened = tools.ledger.opened_sources_payload()
            store.close()

        self.assertIn("PDF", output)
        self.assertIn("pages 4 / 6", output)
        self.assertIn("[page 4]", output)
        self.assertIn("page-specific PDF evidence", output)
        self.assertEqual(tools.sources_read, {url})
        self.assertEqual(opened[0]["content_kind"], "pdf")
        self.assertEqual(opened[0]["pages_read"], [4])
        self.assertEqual(opened[0]["page_count"], 6)

    def test_open_url_skips_pdf_without_extractable_text_without_recording_source(self) -> None:
        url = "https://example.com/scanned.pdf"

        class PdfSearch:
            def fetch(self, requested: str) -> dict:
                return {
                    "url": requested,
                    "title": "Scanned PDF",
                    "text": "",
                    "content_kind": "pdf",
                    "mime_type": "application/pdf",
                    "bytes": b"%PDF fixture",
                    "truncated": False,
                }

        with tempfile.TemporaryDirectory() as td, fake_pypdf("", ""):
            store = KnowledgeStore(Path(td))
            tools = ResearchTools(PdfSearch(), store, KnowledgeChanges(store.root))

            output = tools.open_url(url)
            store.close()

        self.assertTrue(output.startswith("SKIPPED: PDF has no extractable text"))
        self.assertEqual(tools.sources_read, set())
        self.assertFalse(tools.ledger.opened_sources_payload())

    def test_source_search_requires_opened_source_and_finds_late_html_offset(self) -> None:
        url = "https://example.com/long"

        class LongHtmlSearch:
            def __init__(self) -> None:
                self.fetches: list[str] = []

            def fetch(self, requested: str) -> dict:
                self.fetches.append(requested)
                return {
                    "url": requested,
                    "title": "Long HTML",
                    "text": (
                        "Overview only. "
                        + ("filler " * 1200)
                        + "The stable-v2 endpoint appears deep in the HTML source."
                    ),
                    "truncated": False,
                }

        search = LongHtmlSearch()
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            tools = ResearchTools(search, store, KnowledgeChanges(store.root))

            before = tools.source_search(url, "stable-v2 endpoint")
            opened = tools.open_url(url, limit=600)
            found = tools.source_search(url, "stable-v2 endpoint")
            evidence_before_write = len(tools.ledger.evidence_items)
            saved = tools.knowledge_write({
                "type": "fact",
                "title": "Stable endpoint",
                "body": "The stable-v2 endpoint appears deep in the HTML source.",
                "sources": [url],
                "evidence": [{
                    "claim": "The endpoint is stable-v2.",
                    "source_url": url,
                    "excerpt": "stable-v2 endpoint appears deep in the HTML source",
                    "stance": "supports",
                }],
            })
            coverage = tools.ledger.coverage_payload()
            evidence_count = len(tools.ledger.evidence_items)
            store.close()

        self.assertTrue(before.startswith("NEEDS_OPEN:"))
        self.assertIn("[more text available", opened)
        self.assertIn("Locator preview only", found)
        self.assertIn("offset ", found)
        self.assertIn('open_url url="https://example.com/long" offset=', found)
        self.assertEqual(evidence_before_write, 0)
        self.assertIn("saved fact note", saved)
        self.assertEqual(evidence_count, 1)
        self.assertEqual(len(search.fetches), 1)
        self.assertEqual(coverage["source_searches"][0]["query"], "stable-v2 endpoint")

    def test_source_search_finds_chinese_html_phrase(self) -> None:
        url = "https://example.com/zh-long"

        class ChineseHtmlSearch:
            def fetch(self, requested: str) -> dict:
                return {
                    "url": requested,
                    "title": "中文长文",
                    "text": "概览。" + ("背景 " * 800) + "最终建议继续使用稳定端点。",
                    "truncated": False,
                }

        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            tools = ResearchTools(ChineseHtmlSearch(), store, KnowledgeChanges(store.root))

            tools.open_url(url, limit=600)
            found = tools.source_search(url, "稳定端点")
            store.close()

        self.assertIn("Locator preview only", found)
        self.assertIn("稳定端点", found)
        self.assertIn('open_url url="https://example.com/zh-long" offset=', found)

    def test_source_search_pdf_locator_does_not_satisfy_page_evidence_until_page_opened(self) -> None:
        url = "https://example.com/method.pdf"

        class PdfSearch:
            def __init__(self) -> None:
                self.fetches: list[str] = []

            def fetch(self, requested: str) -> dict:
                self.fetches.append(requested)
                return {
                    "url": requested,
                    "title": "Method PDF",
                    "text": "",
                    "content_kind": "pdf",
                    "mime_type": "application/pdf",
                    "bytes": b"%PDF fixture",
                    "truncated": False,
                }

        with tempfile.TemporaryDirectory() as td, fake_pypdf(
            "page one",
            "page two",
            "page three",
            "page four",
            "page five",
            "page six",
            "page seven",
            "page eight",
            "The validation method uses stratified bootstrap validation.",
            "page ten",
        ):
            store = KnowledgeStore(Path(td))
            search = PdfSearch()
            tools = ResearchTools(search, store, KnowledgeChanges(store.root))

            opened = tools.open_url(url)
            located = tools.source_search(url, "stratified bootstrap validation")
            pages_after_search = tools.ledger.opened_sources_payload()[0]["pages_read"]
            evidence_after_search = len(tools.ledger.evidence_items)
            rejected = tools.knowledge_write({
                "type": "fact",
                "title": "Validation method",
                "body": "The validation method uses stratified bootstrap validation.",
                "sources": [url],
                "evidence": [{
                    "claim": "The method uses stratified bootstrap validation.",
                    "source_url": url,
                    "excerpt": "stratified bootstrap validation",
                    "stance": "supports",
                    "page": 9,
                }],
            })
            page = tools.open_url(url, pages="9")
            accepted = tools.knowledge_write({
                "type": "fact",
                "title": "Validation method",
                "body": "The validation method uses stratified bootstrap validation.",
                "sources": [url],
                "evidence": [{
                    "claim": "The method uses stratified bootstrap validation.",
                    "source_url": url,
                    "excerpt": "stratified bootstrap validation",
                    "stance": "supports",
                    "page": 9,
                }],
            })
            pages_after_open = tools.ledger.opened_sources_payload()[0]["pages_read"]
            evidence_count = len(tools.ledger.evidence_items)
            store.close()

        self.assertIn("pages 1-5 / 10", opened)
        self.assertIn("p.9", located)
        self.assertIn('open_url url="https://example.com/method.pdf" pages="9"', located)
        self.assertEqual(pages_after_search, [1, 2, 3, 4, 5])
        self.assertEqual(evidence_after_search, 0)
        self.assertTrue(rejected.startswith("ERROR: evidence cites unread PDF page p.9"))
        self.assertIn("[page 9]", page)
        self.assertIn("saved fact note", accepted)
        self.assertEqual(pages_after_open, [1, 2, 3, 4, 5, 9])
        self.assertEqual(evidence_count, 1)
        self.assertEqual(search.fetches, [url, url, url])

    def test_source_search_pdf_broad_query_scans_before_low_limit_ranking(self) -> None:
        url = "https://example.com/broad-method.pdf"

        class PdfSearch:
            def __init__(self) -> None:
                self.fetches: list[str] = []

            def fetch(self, requested: str) -> dict:
                self.fetches.append(requested)
                return {
                    "url": requested,
                    "title": "Broad Method PDF",
                    "text": "",
                    "content_kind": "pdf",
                    "mime_type": "application/pdf",
                    "bytes": b"%PDF fixture",
                    "truncated": False,
                }

        with tempfile.TemporaryDirectory() as td, fake_pypdf(
            "method overview only",
            "method background only",
            "method appendix only",
            "page four",
            "page five",
            "page six",
            "page seven",
            "page eight",
            "validation method target phrase",
            "page ten",
        ):
            store = KnowledgeStore(Path(td))
            search = PdfSearch()
            tools = ResearchTools(search, store, KnowledgeChanges(store.root))

            tools.open_url(url)
            located = tools.source_search(url, "validation method", limit=3)
            pages_after_search = tools.ledger.opened_sources_payload()[0]["pages_read"]
            store.close()

        self.assertIn("1. p.9", located)
        self.assertIn("validation method target phrase", located)
        self.assertEqual(pages_after_search, [1, 2, 3, 4, 5])
        self.assertEqual(search.fetches, [url, url])

    def test_browser_worker_exposes_module_call_for_research_search(self) -> None:
        self.assertEqual(browser_worker.call(lambda value: value + 1, 4), 5)

    def test_browser_worker_call_is_reentrant_on_browser_thread(self) -> None:
        def outer() -> str:
            return browser_worker.call(lambda: "nested", timeout=0.5)

        self.assertEqual(browser_worker.call(outer, timeout=1.0), "nested")

    def test_browser_worker_call_observes_task_cancellation_while_waiting(self) -> None:
        event = threading.Event()

        def slow() -> str:
            time.sleep(0.3)
            return "done"

        timer = threading.Timer(0.05, event.set)
        timer.start()
        started = time.monotonic()
        try:
            with cancellation.scope(event):
                with self.assertRaises(cancellation.TaskCancelled):
                    browser_worker.call(slow)
        finally:
            timer.cancel()

        self.assertLess(time.monotonic() - started, 0.25)

    def test_final_summary_requires_exact_opened_url_not_same_domain(self) -> None:
        problem = provenance_problem(
            "来源: https://example.com/b",
            opened_sources={"https://example.com/a"},
            search_result_urls=set(),
        )

        self.assertIn("did not open", problem)

    def test_final_summary_bare_domain_allows_opened_subdomain_parent_site(self) -> None:
        problem = provenance_problem(
            "来源质量: [1] primary · official · fresh · python.org",
            opened_sources={"https://docs.python.org/3/library/pathlib.html"},
            search_result_urls=set(),
        )

        self.assertIsNone(problem)

    def test_final_summary_bare_domain_does_not_allow_parent_to_claim_unopened_child(self) -> None:
        problem = provenance_problem(
            "来源质量: [1] primary · official · fresh · docs.python.org",
            opened_sources={"https://www.python.org/"},
            search_result_urls=set(),
        )

        self.assertIn("did not open", problem)

    def test_provenance_can_allow_search_result_mentions_as_limitations(self) -> None:
        problem = provenance_problem(
            "反证与限制: agency.gov and blog.example were search leads, not usable evidence.",
            opened_sources=set(),
            search_result_urls={
                "https://agency.gov/alpha-safety/manual",
                "https://blog.example/alpha-safety-summary",
            },
            allow_search_result_mentions=True,
        )

        self.assertIsNone(problem)

    def test_provenance_treats_fullwidth_parenthesis_as_url_boundary(self) -> None:
        url = "https://example.com/report.pdf"
        problem = provenance_problem(
            f"来源为PDF（{url}），属于一手文献。",
            opened_sources={url},
            search_result_urls={url},
        )

        self.assertIsNone(problem)

    def test_provenance_treats_backtick_as_url_boundary(self) -> None:
        url = "https://example.com/report.pdf"
        problem = provenance_problem(
            f"来源为 `{url}`，属于一手文献。",
            opened_sources={url},
            search_result_urls={url},
        )

        self.assertIsNone(problem)

    def test_report_quality_allows_source_quality_parent_domain_for_opened_subdomain(self) -> None:
        url = "https://docs.python.org/3/library/pathlib.html"
        ledger = ResearchLedger()
        ledger.record_search("python pathlib docs", [{
            "title": "pathlib docs",
            "url": url,
            "snippet": "Python pathlib documentation.",
        }])
        ledger.record_open(
            requested_url=url,
            final_url=url,
            title="pathlib docs",
            text="The pathlib module offers classes representing filesystem paths.",
        )
        evidence = ledger.prepare_evidence_items(
            [{
                "claim": "pathlib provides filesystem path classes.",
                "source_url": url,
                "excerpt": "classes representing filesystem paths",
                "stance": "supports",
            }],
            fallback_sources=[url],
            fallback_claim="pathlib provides filesystem path classes.",
            fallback_body="The pathlib module offers classes representing filesystem paths.",
            note_type="fact",
        )
        self.assertFalse(evidence.error)
        ledger.add_evidence_items(list(evidence.items), note_id="fact-python")
        report = (
            "## 结论\n"
            "- pathlib provides filesystem path classes. [1]\n\n"
            "## 关键证据\n"
            "- [1] The opened docs page says pathlib has classes representing filesystem paths.\n\n"
            "## 反证与限制\n"
            "- 未找到强反证；本轮搜索了 python pathlib docs，若官方文档变更会推翻当前结论。\n\n"
            "## 来源质量\n"
            "- [1] primary · official · fresh · python.org\n\n"
            "## 搜索覆盖\n"
            "- query: python pathlib docs\n"
            "- opened: Python docs\n"
            "- skipped: none representative\n"
            "- stop: official docs directly answer the question\n\n"
            "## 来源\n"
            f"[1] pathlib docs - {url}"
        )

        review = review_report_quality(
            report,
            ledger=ledger,
            opened_sources={url},
            search_result_urls={url},
        )

        self.assertTrue(review.ok, review.message)

    def test_report_quality_requires_counter_section(self) -> None:
        url = "https://example.com/helium"
        ledger = helium_ledger(url)
        report = valid_research_report(url).replace("## 反证与限制\n- 未找到强反证；本轮搜索了 helium，并会被新的 primary supply data 推翻。\n\n", "")

        review = review_report_quality(
            report,
            ledger=ledger,
            opened_sources={url},
            search_result_urls={url},
        )

        self.assertFalse(review.ok)
        self.assertIn("反证", review.message)

    def test_report_quality_rejects_unmapped_citation_number(self) -> None:
        url = "https://example.com/helium"
        ledger = helium_ledger(url)
        report = valid_research_report(url).replace("[1]", "[2]", 1)

        review = review_report_quality(
            report,
            ledger=ledger,
            opened_sources={url},
            search_result_urls={url},
        )

        self.assertFalse(review.ok)
        self.assertIn("[2]", review.message)

    def test_report_quality_rejects_source_url_not_opened_as_final_url(self) -> None:
        url = "https://example.com/final"
        ledger = helium_ledger(url)
        report = valid_research_report("https://example.com/search-result")

        review = review_report_quality(
            report,
            ledger=ledger,
            opened_sources={"https://example.com/search-result", url},
            search_result_urls={"https://example.com/search-result"},
        )

        self.assertFalse(review.ok)
        self.assertIn("final URLs", review.message)

    def test_report_quality_requires_snippet_backed_cited_sources(self) -> None:
        url = "https://example.com/helium"
        ledger = ResearchLedger()
        ledger.record_open(
            requested_url=url,
            final_url=url,
            title="Helium article",
            text="Helium is separated from natural gas streams.",
        )

        review = review_report_quality(
            valid_research_report(url),
            ledger=ledger,
            opened_sources={url},
            search_result_urls={url},
        )

        self.assertFalse(review.ok)
        self.assertIn("evidence snippet", review.message)
        self.assertIn(url, review.message)

    def test_report_quality_accepts_numbered_heading_variants(self) -> None:
        url = "https://example.com/helium"
        ledger = helium_ledger(url)
        report = (
            "## 1. 结论\n"
            "- Helium supply depends on gas processing. [1]\n\n"
            "## 二、关键证据\n"
            "- [1] The opened source says helium is separated from natural gas streams.\n\n"
            "## 三）反证与限制\n"
            "- 未找到强反证；本轮搜索了 helium，并会被新的 primary supply data 推翻。\n\n"
            "## 4. 来源质量\n"
            "- [1] secondary · web · undated · example.com\n\n"
            "## 五、搜索覆盖\n"
            "- query: helium\n\n"
            "## 6. 来源\n"
            f"[1] Helium article - {url}"
        )

        review = review_report_quality(
            report,
            ledger=ledger,
            opened_sources={url},
            search_result_urls={url},
        )

        self.assertTrue(review.ok, review.message)
        self.assertEqual(review.citation_map[0].url, url)

    def test_report_quality_accepts_chinese_text_adjacent_citation(self) -> None:
        url = "https://example.com/helium"
        ledger = helium_ledger(url)
        report = valid_research_report(
            url,
            conclusion="氦供应依赖天然气处理[1]",
        )

        review = review_report_quality(
            report,
            ledger=ledger,
            opened_sources={url},
            search_result_urls={url},
        )

        self.assertTrue(review.ok, review.message)
        self.assertEqual(review.citation_map[0].url, url)

    def test_report_quality_accepts_markdown_link_citations(self) -> None:
        url = "https://example.com/helium"
        ledger = helium_ledger(url)
        report = valid_research_report(url).replace(
            f"[1] Helium article - {url}",
            f"[1] [Helium article]({url})",
        )

        review = review_report_quality(
            report,
            ledger=ledger,
            opened_sources={url},
            search_result_urls={url},
        )

        self.assertTrue(review.ok, review.message)
        self.assertEqual(review.citation_map[0].title, "Helium article")
        self.assertEqual(review.citation_map[0].url, url)

    def test_report_quality_accepts_url_only_source_entry_with_ledger_title(self) -> None:
        url = "https://example.com/helium"
        ledger = helium_ledger(url)
        report = valid_research_report(url).replace(
            f"[1] Helium article - {url}",
            f"[1] {url}",
        )

        review = review_report_quality(
            report,
            ledger=ledger,
            opened_sources={url},
            search_result_urls={url},
        )

        self.assertTrue(review.ok, review.message)
        self.assertEqual(review.citation_map[0].title, "Helium article")

    def test_report_quality_accepts_bibliography_style_source_entry(self) -> None:
        url = "https://example.com/helium"
        ledger = helium_ledger(url)
        report = valid_research_report(url).replace(
            f"[1] Helium article - {url}",
            f"1. Helium article. Available at: {url}",
        )

        review = review_report_quality(
            report,
            ledger=ledger,
            opened_sources={url},
            search_result_urls={url},
        )

        self.assertTrue(review.ok, review.message)
        self.assertEqual(review.citation_map[0].title, "Helium article")

    def test_report_quality_accepts_numbered_url_first_source_entry(self) -> None:
        url = "https://example.com/helium"
        ledger = helium_ledger(url)
        report = valid_research_report(url).replace(
            f"[1] Helium article - {url}",
            f"1. {url} - Helium article",
        )

        review = review_report_quality(
            report,
            ledger=ledger,
            opened_sources={url},
            search_result_urls={url},
        )

        self.assertTrue(review.ok, review.message)
        self.assertEqual(review.citation_map[0].title, "Helium article")
        self.assertEqual(review.citation_map[0].url, url)

    def test_report_quality_allows_no_citable_source_report_for_failed_search_leads(self) -> None:
        ledger = ResearchLedger()
        ledger.record_search("Alpha Safety Program 72 hour threshold", [
            {
                "title": "Alpha Safety Program official manual",
                "url": "https://agency.gov/alpha-safety/manual",
                "snippet": "Official manual.",
            },
            {
                "title": "Alpha Safety summary blog",
                "url": "https://blog.example/alpha-safety-summary",
                "snippet": "Secondary summary.",
            },
        ])
        report = (
            "## 结论\n"
            "未能确认 Alpha Safety Program 要求 72 小时事件通知阈值。\n\n"
            "## 关键证据\n"
            "无可引用的有效来源；搜索结果里的 agency.gov 和 blog.example 没有提供可验证正文。\n\n"
            "## 反证与限制\n"
            "未找到强反证。限制是 agency.gov 和 blog.example 只是搜索线索，未形成可引用证据。\n\n"
            "## 来源质量\n"
            "无有效来源；没有可引用的已打开页面。\n\n"
            "## 搜索覆盖\n"
            "搜索了 Alpha Safety Program、72 hour threshold、agency.gov 和 blog.example 相关线索。\n\n"
            "## 来源\n"
            "本报告无可引用的已打开有效来源。"
        )

        review = review_report_quality(
            report,
            ledger=ledger,
            opened_sources=set(),
            search_result_urls={
                "https://agency.gov/alpha-safety/manual",
                "https://blog.example/alpha-safety-summary",
            },
        )

        self.assertTrue(review.ok, review.message)
        self.assertIn("no opened source", review.warnings[0])

    def test_report_quality_rejects_no_citable_report_that_lists_unopened_url_as_source(self) -> None:
        ledger = ResearchLedger()
        ledger.record_search("Alpha Safety Program", [{
            "title": "Alpha Safety Program official manual",
            "url": "https://agency.gov/alpha-safety/manual",
            "snippet": "Official manual.",
        }])
        report = (
            "## 结论\n"
            "未能确认 Alpha Safety Program 要求 72 小时事件通知阈值。\n\n"
            "## 关键证据\n"
            "无可引用的有效来源。\n\n"
            "## 反证与限制\n"
            "未找到强反证；搜索了 agency.gov。\n\n"
            "## 来源质量\n"
            "无有效来源。\n\n"
            "## 搜索覆盖\n"
            "搜索了 Alpha Safety Program。\n\n"
            "## 来源\n"
            "[1] https://agency.gov/alpha-safety/manual"
        )

        review = review_report_quality(
            report,
            ledger=ledger,
            opened_sources=set(),
            search_result_urls={"https://agency.gov/alpha-safety/manual"},
        )

        self.assertFalse(review.ok)
        self.assertIn("did not open", review.message)

    def test_report_quality_accepts_pdf_page_citations_with_page_backed_evidence(self) -> None:
        url = "https://example.com/report.pdf"
        ledger = pdf_ledger(url)
        report = (
            "## 结论\n"
            "- PDF intake supports page-specific evidence. [1 p.4]\n\n"
            "## 关键证据\n"
            "- [1 p.4] The opened PDF page contains the page-specific evidence statement.\n\n"
            "## 反证与限制\n"
            "- 未找到强反证；本轮搜索了 report pdf，若 PDF 第 4 页原文不同会推翻当前结论。\n\n"
            "## 来源质量\n"
            "- [1] secondary · data · undated · example.com\n\n"
            "## 搜索覆盖\n"
            "- query: report pdf\n"
            "- opened: PDF p.4\n"
            "- stop: page-level evidence covers the fixture\n\n"
            "## 来源\n"
            f"[1] Report PDF - {url}"
        )

        review = review_report_quality(
            report,
            ledger=ledger,
            opened_sources={url},
            search_result_urls={url},
        )

        self.assertTrue(review.ok, review.message)
        self.assertEqual(review.citation_map[0].pages, (4,))
        self.assertEqual(review.citation_payload()[0]["pages"], [4])

    def test_report_quality_rejects_pdf_page_citation_when_page_was_not_read(self) -> None:
        url = "https://example.com/report.pdf"
        ledger = pdf_ledger(url)
        report = (
            "## 结论\n"
            "- PDF intake supports page-specific evidence. [1 p.5]\n\n"
            "## 关键证据\n"
            "- [1 p.5] The opened PDF page contains the page-specific evidence statement.\n\n"
            "## 反证与限制\n"
            "- 未找到强反证；本轮搜索了 report pdf，若 PDF 第 5 页原文不同会推翻当前结论。\n\n"
            "## 来源质量\n"
            "- [1] secondary · data · undated · example.com\n\n"
            "## 搜索覆盖\n"
            "- query: report pdf\n\n"
            "## 来源\n"
            f"[1] Report PDF - {url}"
        )

        review = review_report_quality(
            report,
            ledger=ledger,
            opened_sources={url},
            search_result_urls={url},
        )

        self.assertFalse(review.ok)
        self.assertIn("p.5", review.message)
        self.assertIn("not read", review.message)

    def test_report_quality_accepts_pdf_page_range_when_pages_were_read(self) -> None:
        url = "https://example.com/report.pdf"
        ledger = ResearchLedger()
        ledger.record_open_document(SourceDocument(
            requested_url=url,
            final_url=url,
            title="Report PDF",
            content_kind="pdf",
            mime_type="application/pdf",
            text=(
                "[page 4]\nThe fourth page contains page-specific PDF evidence.\n\n"
                "[page 5]\nThe fifth page adds context."
            ),
            page_count=12,
            pages_read=(4, 5),
            page_texts=(
                SourcePage(number=4, text="The fourth page contains page-specific PDF evidence."),
                SourcePage(number=5, text="The fifth page adds context."),
            ),
        ))
        evidence = ledger.prepare_evidence_items(
            [{
                "claim": "PDF intake supports page-specific evidence.",
                "source_url": url,
                "excerpt": "page-specific PDF evidence",
                "stance": "supports",
                "page": 4,
            }],
            fallback_sources=[url],
            fallback_claim="PDF intake supports page-specific evidence.",
            fallback_body="The fourth page contains page-specific PDF evidence.",
            note_type="fact",
        )
        self.assertFalse(evidence.error)
        ledger.add_evidence_items(list(evidence.items), note_id="fact-pdf")
        report = (
            "## 结论\n"
            "- PDF intake supports page-specific evidence. [1 pp.4-5]\n\n"
            "## 关键证据\n"
            "- [1 pp.4-5] The opened PDF pages contain the evidence and context.\n\n"
            "## 反证与限制\n"
            "- 未找到强反证；本轮搜索了 report pdf，若 PDF 第 4-5 页原文不同会推翻当前结论。\n\n"
            "## 来源质量\n"
            "- [1] secondary · data · undated · example.com\n\n"
            "## 搜索覆盖\n"
            "- query: report pdf\n\n"
            "## 来源\n"
            f"[1] Report PDF - {url}"
        )

        review = review_report_quality(
            report,
            ledger=ledger,
            opened_sources={url},
            search_result_urls={url},
        )

        self.assertTrue(review.ok, review.message)
        self.assertEqual(review.citation_map[0].pages, (4, 5))

    def test_report_quality_keeps_pdf_pages_read_across_multiple_opens(self) -> None:
        url = "https://example.com/report.pdf"
        ledger = ResearchLedger()
        ledger.record_open_document(SourceDocument(
            requested_url=url,
            final_url=url,
            title="Report PDF",
            content_kind="pdf",
            mime_type="application/pdf",
            text="[page 4]\nThe fourth page contains page-specific PDF evidence.",
            page_count=12,
            pages_read=(4,),
            page_texts=(
                SourcePage(number=4, text="The fourth page contains page-specific PDF evidence."),
            ),
        ))
        evidence = ledger.prepare_evidence_items(
            [{
                "claim": "PDF intake supports page-specific evidence.",
                "source_url": url,
                "excerpt": "page-specific PDF evidence",
                "stance": "supports",
                "page": 4,
            }],
            fallback_sources=[url],
            fallback_claim="PDF intake supports page-specific evidence.",
            fallback_body="The fourth page contains page-specific PDF evidence.",
            note_type="fact",
        )
        self.assertFalse(evidence.error)
        ledger.add_evidence_items(list(evidence.items), note_id="fact-pdf")
        ledger.record_open_document(SourceDocument(
            requested_url=url,
            final_url=url,
            title="Report PDF",
            content_kind="pdf",
            mime_type="application/pdf",
            text="[page 5]\nThe fifth page adds context.",
            page_count=12,
            pages_read=(5,),
            page_texts=(SourcePage(number=5, text="The fifth page adds context."),),
        ))
        report = (
            "## 结论\n"
            "- PDF intake supports page-specific evidence. [1 p.4]\n\n"
            "## 关键证据\n"
            "- [1 p.4] The opened PDF page contains the evidence statement.\n\n"
            "## 反证与限制\n"
            "- 未找到强反证；本轮搜索了 report pdf，若 PDF 第 4 页原文不同会推翻当前结论。\n\n"
            "## 来源质量\n"
            "- [1] secondary · data · undated · example.com\n\n"
            "## 搜索覆盖\n"
            "- query: report pdf\n\n"
            "## 来源\n"
            f"[1] Report PDF - {url}"
        )

        review = review_report_quality(
            report,
            ledger=ledger,
            opened_sources={url},
            search_result_urls={url},
        )

        self.assertTrue(review.ok, review.message)
        self.assertEqual(ledger.opened_sources_payload()[0]["pages_read"], [4, 5])

    def test_report_quality_extracts_citation_counterpoints_and_warnings(self) -> None:
        url = "https://example.com/helium"
        ledger = helium_ledger(url)

        review = review_report_quality(
            valid_research_report(url),
            ledger=ledger,
            opened_sources={url},
            search_result_urls={url},
        )

        self.assertTrue(review.ok, review.message)
        self.assertEqual(review.citation_map[0].url, url)
        self.assertTrue(review.counterpoints)
        self.assertIn("only one cited source", "\n".join(review.warnings))

    def test_ledger_tracks_search_coverage_and_opened_results(self) -> None:
        ledger = ResearchLedger()
        ledger.record_search("helium supply", [
            {"title": "Helium article", "url": "https://example.com/helium", "snippet": "supply note"},
            {"title": "Other", "url": "https://example.com/other", "snippet": "other"},
        ])
        ledger.record_open(
            requested_url="https://example.com/helium",
            final_url="https://example.com/helium",
            title="Helium article",
            text="Helium is separated from natural gas streams.",
        )

        coverage = ledger.coverage_payload()
        payload = ledger.search_results_payload()

        self.assertEqual(coverage["queries"], ["helium supply"])
        self.assertEqual(payload[0]["query"], "helium supply")
        self.assertTrue(payload[0]["opened"])
        self.assertEqual(coverage["opened_count"], 1)
        self.assertTrue(coverage["skipped_results"])

    def test_fact_note_requires_opened_source(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            tools = ResearchTools(FakeSearch(), store, KnowledgeChanges(store.root))

            rejected = tools.knowledge_write({
                "type": "fact",
                "title": "Helium",
                "body": "Helium is useful.",
                "sources": ["https://example.com/helium"],
            })
            tools.sources_read.add("https://example.com/helium")
            accepted = tools.knowledge_write({
                "type": "fact",
                "title": "Helium",
                "body": "Helium is useful.",
                "sources": ["https://example.com/helium"],
            })
            store.close()

        self.assertTrue(rejected.startswith("ERROR:"))
        self.assertIn("saved fact note", accepted)

    def test_search_result_source_requires_open_without_rendering_as_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            tools = ResearchTools(FakeSearch(), store, KnowledgeChanges(store.root))
            tools.search_result_urls.add("https://example.com/helium")

            result = tools.knowledge_write({
                "type": "fact",
                "title": "Helium",
                "body": "Helium is useful.",
                "sources": ["https://example.com/helium"],
            })
            count = store.index.count()
            store.close()

        self.assertTrue(result.startswith("NEEDS_OPEN:"))
        self.assertIn("open_url", result)
        self.assertEqual(count, 0)

    def test_needs_open_outcome_is_not_changed_or_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            runner = ResearchRunner(FakeProvider(), FakeSearch(), store, max_turns=2)
            runner.tools.search_result_urls.add("https://example.com/helium")
            call = ToolCall("knowledge_write", {
                "type": "fact",
                "title": "Helium",
                "body": "Helium is useful.",
                "sources": ["https://example.com/helium"],
            })

            outcome = runner._dispatch(call)
            payload = run_event_payload(RunEvent.tool_finished(1, call, outcome))
            count = store.index.count()
            store.close()

        self.assertEqual(outcome.status, "needs_action")
        self.assertTrue(outcome.ok)
        self.assertFalse(outcome.changed)
        self.assertEqual(count, 0)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["status"], "needs_action")
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["changed"])

    def test_saved_note_outcome_is_changed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            runner = ResearchRunner(FakeProvider(), FakeSearch(), store, max_turns=2)
            runner.tools.sources_read.add("https://example.com/helium")
            call = ToolCall("knowledge_write", {
                "type": "fact",
                "title": "Helium",
                "body": "Helium is useful.",
                "sources": ["https://example.com/helium"],
            })

            outcome = runner._dispatch(call)
            payload = run_event_payload(RunEvent.tool_finished(1, call, outcome))
            count = store.index.count()
            store.close()

        self.assertEqual(outcome.status, "ok")
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.changed)
        self.assertEqual(count, 1)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["changed"])

    def test_web_search_accepts_single_query_from_queries_alias(self) -> None:
        search = RecordingSearch()
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            runner = ResearchRunner(FakeProvider(), search, store, max_turns=2)
            call = ToolCall("web_search", {"queries": ["helium supply", "argon"]})

            outcome = runner._dispatch(call)
            store.close()

        self.assertTrue(outcome.ok)
        self.assertEqual(search.queries, ["helium supply"])
        self.assertIn("Helium article", outcome.output)

    def test_source_search_dispatch_accepts_queries_alias(self) -> None:
        url = "https://example.com/helium"
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            runner = ResearchRunner(FakeProvider(), FakeSearch(), store, max_turns=2)
            runner.tools.open_url(url)
            call = ToolCall("source_search", {"url": url, "queries": ["natural gas", "argon"]})

            outcome = runner._dispatch(call)
            store.close()

        self.assertTrue(outcome.ok)
        self.assertIn("natural gas", outcome.output)

    def test_unsupported_content_type_is_skipped_not_failed(self) -> None:
        class PdfSearch:
            def fetch(self, url: str) -> dict:
                return {
                    "url": url,
                    "title": "",
                    "text": "ERROR: unsupported content type: application/pdf",
                    "truncated": False,
                }

        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            runner = ResearchRunner(FakeProvider(), PdfSearch(), store, max_turns=2)
            call = ToolCall("open_url", {"url": "https://example.com/report.pdf"})

            outcome = runner._dispatch(call)
            payload = run_event_payload(RunEvent.tool_finished(1, call, outcome))
            store.close()

        self.assertTrue(outcome.output.startswith("SKIPPED: unsupported content type: application/pdf"))
        self.assertEqual(outcome.status, "needs_action")
        self.assertTrue(outcome.ok)
        self.assertFalse(outcome.changed)
        self.assertEqual(runner.tools.sources_read, set())
        self.assertEqual(payload["status"], "needs_action")
        self.assertTrue(payload["ok"])

    def test_invalid_evidence_excerpt_is_replaced_with_opened_page_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            tools = ResearchTools(FakeSearch(), store, KnowledgeChanges(store.root))
            url = "https://example.com/helium"
            tools.sources_read.add(url)
            tools.ledger.record_open(
                requested_url=url,
                final_url=url,
                title="Helium article",
                text="Helium is separated from natural gas streams.",
            )

            saved = tools.knowledge_write({
                "type": "fact",
                "title": "Helium",
                "body": "Helium is useful.",
                "sources": [url],
                "evidence": [{
                    "claim": "Helium is useful.",
                    "source_url": url,
                    "excerpt": "This sentence was never opened.",
                    "stance": "supports",
                }],
            })
            accepted = tools.knowledge_write({
                "type": "fact",
                "title": "Helium source",
                "body": "Helium comes from gas processing.",
                "sources": [url],
                "evidence": [{
                    "claim": "Helium comes from gas processing.",
                    "source_url": url,
                    "excerpt": "Helium is separated from natural gas streams.",
                    "stance": "supports",
                }],
            })
            store.close()

        self.assertIn("saved fact note", saved)
        self.assertIn("WARNING:", saved)
        self.assertIn("attached an exact opened-page excerpt", saved)
        self.assertIn("saved fact note", accepted)
        self.assertEqual(len(tools.ledger.evidence_items), 2)
        self.assertEqual(
            tools.ledger.evidence_items[0].excerpt,
            "Helium is separated from natural gas streams.",
        )

    def test_pdf_evidence_page_is_inferred_and_bad_excerpt_replaced_on_that_page(self) -> None:
        url = "https://example.com/report.pdf"
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            tools = ResearchTools(FakeSearch(), store, KnowledgeChanges(store.root))
            tools.sources_read.add(url)
            tools.ledger.record_open_document(SourceDocument(
                requested_url=url,
                final_url=url,
                title="Report PDF",
                content_kind="pdf",
                mime_type="application/pdf",
                text="[page 4]\nThe fourth page contains page-specific PDF evidence.",
                page_count=8,
                pages_read=(4,),
                page_texts=(SourcePage(
                    number=4,
                    text="The fourth page contains page-specific PDF evidence.",
                ),),
            ))

            inferred = tools.knowledge_write({
                "type": "fact",
                "title": "PDF page evidence",
                "body": "The fourth page contains page-specific PDF evidence.",
                "sources": [url],
                "evidence": [{
                    "claim": "The PDF has page-specific evidence.",
                    "source_url": url,
                    "excerpt": "page-specific PDF evidence",
                    "stance": "supports",
                }],
            })
            replaced = tools.knowledge_write({
                "type": "fact",
                "title": "PDF page replacement",
                "body": "The fourth page contains page-specific PDF evidence.",
                "sources": [url],
                "evidence": [{
                    "claim": "The PDF has page-specific evidence.",
                    "source_url": url,
                    "excerpt": "not in the PDF",
                    "stance": "supports",
                    "page": 4,
                }],
            })
            store.close()

        self.assertIn("saved fact note", inferred)
        self.assertIn("saved fact note", replaced)
        self.assertIn("WARNING:", replaced)
        self.assertEqual(tools.ledger.evidence_items[0].page, 4)
        self.assertEqual(tools.ledger.evidence_items[0].locator, "p.4")
        self.assertEqual(tools.ledger.evidence_items[1].page, 4)

    def test_research_protocol_guides_open_url_before_note_write(self) -> None:
        codec = JsonToolCodec()
        baseline_codec = JsonToolCodec(include_source_search=False)
        prompt = codec.system_prompt()
        baseline_prompt = baseline_codec.system_prompt()
        repair = codec.repair_prompt()
        followup = codec.format_results([
            ToolResult(
                ToolCall("knowledge_write", {"title": "Helium"}),
                "NEEDS_OPEN: open_url before saving this note: https://example.com/helium",
            )
        ])

        self.assertIn("A web_search result is not evidence yet", prompt)
        self.assertIn("call open_url", prompt)
        self.assertIn("exact short excerpts copied from open_url text", prompt)
        self.assertIn('"pages":"1-5"', prompt)
        self.assertIn("open_url can read text PDFs", prompt)
        self.assertIn("source_search", prompt)
        self.assertIn("locator previews", prompt)
        self.assertIn("open the returned offset", prompt)
        self.assertNotIn("source_search", baseline_prompt)
        self.assertIn("Research hard boundary", prompt)
        self.assertIn("Do not write the research answer directly", prompt)
        self.assertIn("Choose exactly one tool", prompt)
        self.assertIn("Do not use this chat website's built-in web search", prompt)
        self.assertIn("Tool outputs are the only evidence", prompt)
        self.assertIn("Choose exactly one tool", repair)
        self.assertIn("Choose exactly one tool", followup)
        self.assertIn("Do not use this chat website's built-in web search", repair)
        self.assertIn("Do not use this chat website's built-in web search", followup)
        self.assertIn("evidence.page", prompt)
        self.assertIn("[1 p.4]", prompt)
        self.assertIn("Do not paraphrase evidence.excerpt", prompt)
        self.assertIn("omit the evidence field", prompt)
        self.assertIn("You are a local research agent", prompt)
        self.assertNotIn("CodeyResearch", prompt)
        self.assertIn("反证与限制", prompt)
        self.assertIn("NEEDS_OPEN", followup)
        self.assertIn("call open_url", followup)

    def test_research_protocol_rejects_multiple_tool_calls_per_reply(self) -> None:
        plan = JsonToolCodec().parse(
            "\n".join([
                json.dumps({"tool": "knowledge_search", "args": {"query": "alpha"}}),
                json.dumps({"tool": "web_search", "args": {"query": "alpha"}}),
            ])
        )

        self.assertFalse(plan.calls)
        self.assertIsNone(plan.control)
        self.assertIn("too many JSON tool calls", plan.protocol_error)

    def test_research_protocol_rejects_duplicate_done_calls_per_reply(self) -> None:
        plan = JsonToolCodec().parse(
            "\n".join([
                json.dumps({"tool": "done", "args": {"answer": "first"}}),
                json.dumps({"tool": "done", "args": {"answer": "second"}}),
            ])
        )

        self.assertFalse(plan.calls)
        self.assertIsNone(plan.control)
        self.assertIn("too many JSON tool calls", plan.protocol_error)

    def test_quality_review_followup_is_specific_when_done_answer_needs_revision(self) -> None:
        url = "https://example.com/helium"
        invalid = valid_research_report(url).replace(
            "Helium supply depends on gas processing. [1]",
            "Helium supply depends on gas processing.",
        )
        provider = FakeProvider(
            json.dumps({"tool": "open_url", "args": {"url": url}}),
            json.dumps({
                "tool": "knowledge_write",
                "args": {
                    "type": "fact",
                    "title": "Helium source",
                    "body": "Helium comes from gas processing.",
                    "sources": [url],
                },
            }),
            json.dumps({"tool": "done", "args": {"answer": invalid}}),
            json.dumps({"tool": "done", "args": {"answer": valid_research_report(url)}}),
        )
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            runner = ResearchRunner(provider, FakeSearch(), store, max_turns=6)

            list(runner.run("Research helium"))
            result = runner.result
            store.close()

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.stop_reason, "done")
        self.assertIn("Your last done.answer did not pass", provider.sent[3])
        self.assertIn("Supported conclusions need [n]", provider.sent[3])
        self.assertNotIn("[no tool output]", provider.sent[3])

    def test_research_advisors_get_only_the_evidence_pack(self) -> None:
        advisor = FakeAdvisorProvider("gap found")
        pack = EvidencePack(
            question="Research helium",
            draft="Draft answer",
            opened_urls=("https://example.com/helium",),
            search_result_urls=("https://example.com/search",),
        )

        reports = run_research_advisors(
            selected_provider_id="deepseek",
            provider_ids=("deepseek", "qwen"),
            provider_labels={"deepseek": "DeepSeek", "qwen": "Qwen"},
            availability=lambda: {"qwen": True},
            connect_existing=lambda _provider_id: advisor,
            pack=pack,
        )

        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].text, "gap found")
        self.assertEqual(advisor.new_chat_calls, 1)
        self.assertTrue(advisor.closed)
        self.assertIn("Research EvidencePack", advisor.sent[0])
        self.assertIn("https://example.com/helium", advisor.sent[0])
        self.assertIn("https://example.com/search", advisor.sent[0])

    def test_research_tools_default_written_notes_to_current_session(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            tools = ResearchTools(
                FakeSearch(),
                store,
                KnowledgeChanges(store.root),
                session_id="s1",
                project="E:/project",
            )
            url = "https://example.com/helium"
            tools.sources_read.add(url)

            saved = tools.knowledge_write({
                "type": "fact",
                "title": "Helium",
                "body": "Helium is useful.",
                "sources": [url],
                "session_id": "attacker",
                "project": "E:/other",
            })
            rows = store.index.recent(5, session_id="s1")
            note = store.read_note(rows[0]["id"])
            store.close()

        self.assertIn("saved fact note", saved)
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(note)
        assert note is not None
        self.assertEqual(note.session_id, "s1")
        self.assertEqual(note.project, "E:/project")

    def test_runner_writes_synthesis_and_restore_can_revert_run(self) -> None:
        url = "https://example.com/helium"
        provider = FakeProvider(
            json.dumps({"tool": "web_search", "args": {"query": "helium"}}),
            json.dumps({"tool": "open_url", "args": {"url": url}}),
            json.dumps({
                "tool": "knowledge_write",
                "args": {
                    "type": "fact",
                    "title": "Helium source",
                    "body": "Helium comes from gas processing.",
                    "sources": [url],
                },
            }),
            json.dumps({
                "tool": "done",
                "args": {"answer": valid_research_report(url)},
            }),
        )
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            runner = ResearchRunner(
                provider,
                FakeSearch(),
                store,
                session_id="s1",
                max_turns=8,
            )

            events = list(runner.run("Research helium"))
            result = runner.result
            synthesis = store.read_note(result.synthesis_id if result else "")
            links = store.index.links_for([result.synthesis_id] if result else [])
            restore = runner.changes.restore_result()
            store.rebuild()
            count = store.index.count()
            store.close()

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.stop_reason, "done")
        self.assertTrue(result.synthesis_id)
        self.assertIn(result.synthesis_id, result.notes_created)
        self.assertEqual(result.source_urls, [url])
        self.assertEqual(result.queries, ["helium"])
        self.assertTrue(result.citation_map)
        self.assertTrue(result.opened_sources)
        self.assertTrue(result.evidence_items)
        self.assertIsNotNone(synthesis)
        assert synthesis is not None
        self.assertIn("Evidence Ledger", synthesis.body)
        self.assertTrue(any(link["kind"] == "derives" for link in links))
        self.assertTrue(any(event.kind == "tool" for event in events))
        self.assertTrue(restore.ok)
        self.assertEqual(count, 0)

    def test_research_provider_send_observes_stop_while_provider_is_blocked(self) -> None:
        stop = threading.Event()
        send_started = threading.Event()
        send_finished = threading.Event()
        release_send = threading.Event()
        runner_returned = threading.Event()
        errors: list[BaseException] = []
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            runner = ResearchRunner(
                SlowProvider(0.0, started=send_started, finished=send_finished, release=release_send),
                FakeSearch(),
                store,
                should_stop=stop.is_set,
                max_turns=2,
            )

            def run_research() -> None:
                try:
                    list(runner.run("Research a slow endpoint"))
                except BaseException as exc:  # pragma: no cover - surfaced below
                    errors.append(exc)
                finally:
                    runner_returned.set()

            thread = threading.Thread(target=run_research, name="test-research-runner")
            thread.start()
            self.assertTrue(send_started.wait(1.0))
            stop.set()
            self.assertTrue(runner_returned.wait(1.0))
            self.assertFalse(send_finished.is_set())
            release_send.set()
            self.assertTrue(send_finished.wait(1.0))
            thread.join(timeout=1.0)
            try:
                self.assertFalse(errors)
                self.assertFalse(thread.is_alive())
                self.assertIsNotNone(runner.result)
                assert runner.result is not None
                self.assertEqual(runner.result.stop_reason, "stopped")
            finally:
                store.close()

    def test_web_style_provider_send_stays_on_runner_thread(self) -> None:
        class ThreadRecordingProvider(FakeProvider):
            def __init__(self) -> None:
                super().__init__(json.dumps({"tool": "done", "args": {"answer": "done"}}))
                self.thread_ids: list[int] = []

            def send(self, text: str, timeout=None) -> str:
                self.thread_ids.append(threading.get_ident())
                return super().send(text, timeout=timeout)

        provider = ThreadRecordingProvider()
        caller_thread_id = threading.get_ident()
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            runner = ResearchRunner(provider, FakeSearch(), store, max_turns=1)

            list(runner.run("Research thread model"))
            store.close()

        self.assertEqual(provider.thread_ids, [caller_thread_id])

    def test_research_intro_includes_bounded_chat_handoff(self) -> None:
        provider = FakeProvider(json.dumps({"tool": "done", "args": {"answer": "done"}}))
        handoff = '{"goal":"Compare SQLite and flat files","latest_reply":"Use SQLite."}'
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            runner = ResearchRunner(
                provider,
                FakeSearch(),
                store,
                chat_handoff=handoff,
                max_turns=2,
            )

            list(runner.run("Research the prior plan"))
            store.close()

        self.assertIn("Conversation context from this chat", provider.sent[0])
        self.assertIn("Compare SQLite", provider.sent[0])

    def test_runner_uses_private_advisors_before_final_research_answer(self) -> None:
        url = "https://example.com/helium"
        provider = FakeProvider(
            json.dumps({"tool": "web_search", "args": {"query": "helium"}}),
            json.dumps({"tool": "open_url", "args": {"url": url}}),
            json.dumps({
                "tool": "knowledge_write",
                "args": {
                    "type": "fact",
                    "title": "Helium source",
                    "body": "Helium comes from gas processing.",
                    "sources": [url],
                },
            }),
            json.dumps({
                "tool": "done",
                "args": {"answer": valid_research_report(url, conclusion="Initial helium conclusion.")},
            }),
            json.dumps({
                "tool": "done",
                "args": {"answer": valid_research_report(url, conclusion="Revised helium conclusion.")},
            }),
        )
        seen: list[EvidencePack] = []

        def review(pack: EvidencePack):
            seen.append(pack)
            return (ConsensusAdvice("qwen", "Qwen", "Need a direct evidence citation."),)

        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            runner = ResearchRunner(
                provider,
                FakeSearch(),
                store,
                session_id="s1",
                max_turns=8,
                review_advisors=review,
            )

            list(runner.run("Research helium"))
            result = runner.result
            store.close()

        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("Revised helium conclusion", result.summary)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].opened_urls, (url,))
        self.assertTrue(seen[0].citation_map)
        self.assertTrue(seen[0].coverage)
        self.assertEqual(len(seen[0].notes), 1)
        self.assertIn("Need a direct evidence citation.", provider.sent[-1])

    def test_runner_synthesis_records_opened_sources_for_project_brief(self) -> None:
        url = "https://example.com/helium"
        provider = FakeProvider(
            json.dumps({"tool": "open_url", "args": {"url": url}}),
            json.dumps({
                "tool": "knowledge_write",
                "args": {
                    "type": "fact",
                    "title": "Helium source",
                    "body": "Helium comes from gas processing.",
                    "sources": [url],
                },
            }),
            json.dumps({
                "tool": "done",
                "args": {"answer": valid_research_report(url)},
            }),
        )
        with tempfile.TemporaryDirectory() as td:
            store = KnowledgeStore(Path(td))
            runner = ResearchRunner(provider, FakeSearch(), store, session_id="s1", max_turns=4)

            list(runner.run("Research helium"))
            result = runner.result
            synthesis = store.read_note(result.synthesis_id if result else "")
            store.close()

        self.assertIsNotNone(result)
        self.assertIsNotNone(synthesis)
        assert synthesis is not None
        self.assertEqual(synthesis.sources, [url])

    def test_browser_search_fetch_uses_separate_page_from_search_results(self) -> None:
        class FakeContext:
            def __init__(self) -> None:
                self.pages = []

            def new_page(self):
                page = FakePage(self)
                self.pages.append(page)
                return page

        class FakePage:
            def __init__(self, context) -> None:
                self.context = context
                self.routes = []
                self.timeout = None
                self.brought_to_front = False

            def is_closed(self) -> bool:
                return False

            def set_default_navigation_timeout(self, timeout) -> None:
                self.timeout = timeout

            def route(self, pattern, handler) -> None:
                self.routes.append((pattern, handler))

            def bring_to_front(self) -> None:
                self.brought_to_front = True

        context = FakeContext()
        search_page = FakePage(context)
        context.pages.append(search_page)
        session = type("Session", (), {
            "page": search_page,
            "browser": type("Browser", (), {"contexts": [context]})(),
        })()
        provider = BrowserSearchProvider()
        provider._session = session
        provider._search_page = search_page

        fetch_page = provider._ensure_fetch_page_on_browser_thread("https://example.com/article")

        self.assertIsNot(fetch_page, search_page)
        self.assertIn(fetch_page, context.pages)
        self.assertTrue(fetch_page.brought_to_front)

    def test_browser_search_fetch_streams_known_pdf_without_opening_browser_page(self) -> None:
        class StreamingResponse:
            headers = {
                "content-type": "application/pdf",
                "content-length": str(PDF_MAX_BYTES + 1),
            }
            read_calls = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def geturl(self) -> str:
                return "https://example.com/report.pdf"

            def read(self, size=-1) -> bytes:
                self.read_calls += 1
                return b"unexpected"

        response = StreamingResponse()
        provider = BrowserSearchProvider()
        provider._ensure_fetch_page_on_browser_thread = mock.Mock(side_effect=AssertionError("PDF should not open a browser page"))

        with (
            mock.patch("codey.research.browser_search.check_fetch_url", return_value=None),
            mock.patch("codey.research.browser_search._open_url_no_redirect", return_value=response),
        ):
            result = provider.fetch("https://example.com/report.pdf")

        self.assertEqual(result["content_kind"], "pdf")
        self.assertTrue(result["text"].startswith("SKIPPED: PDF is too large"))
        self.assertEqual(response.read_calls, 0)
        provider._ensure_fetch_page_on_browser_thread.assert_not_called()

    def test_browser_search_pdf_streaming_stops_after_cap_without_content_length(self) -> None:
        class StreamingResponse:
            headers = {"content-type": "application/pdf"}

            def __init__(self) -> None:
                self.bytes_sent = 0
                self.total_bytes = 10 * 1024 * 1024

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def geturl(self) -> str:
                return "https://example.com/report.pdf"

            def read(self, size=-1) -> bytes:
                if self.bytes_sent >= self.total_bytes:
                    return b""
                amount = min(size if size > 0 else self.total_bytes, self.total_bytes - self.bytes_sent)
                self.bytes_sent += amount
                return b"x" * amount

        response = StreamingResponse()
        provider = BrowserSearchProvider()

        with (
            mock.patch("codey.research.browser_search.check_fetch_url", return_value=None),
            mock.patch("codey.research.browser_search.PDF_MAX_BYTES", 1024),
            mock.patch("codey.research.browser_search._open_url_no_redirect", return_value=response),
        ):
            result = provider.fetch("https://example.com/report.pdf")

        self.assertEqual(result["content_kind"], "pdf")
        self.assertTrue(result["text"].startswith("SKIPPED: PDF is too large"))
        self.assertLess(response.bytes_sent, response.total_bytes)

    def test_browser_search_pdf_redirect_is_checked_before_following_private_target(self) -> None:
        class RedirectResponse:
            status = 302
            headers = {
                "location": "http://127.0.0.1/private.pdf",
                "content-type": "application/pdf",
            }
            read_calls = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def geturl(self) -> str:
                return "https://example.com/report.pdf"

            def getcode(self) -> int:
                return 302

            def read(self, size=-1) -> bytes:
                self.read_calls += 1
                return b""

        response = RedirectResponse()
        provider = BrowserSearchProvider()

        def policy(url: str, *, resolve: bool = True):
            del resolve
            if url.startswith("http://127.0.0.1"):
                return "non-public target"
            return None

        with (
            mock.patch("codey.research.browser_search.check_fetch_url", side_effect=policy),
            mock.patch("codey.research.browser_search._open_url_no_redirect", return_value=response) as opened,
        ):
            result = provider.fetch("https://example.com/report.pdf")

        self.assertEqual(result["text"], "ERROR: non-public target (after redirect)")
        self.assertEqual(response.read_calls, 0)
        self.assertEqual(opened.call_count, 1)

    def test_browser_search_pdf_http_error_redirect_is_checked_before_following_private_target(self) -> None:
        class RedirectHandler(BaseHTTPRequestHandler):
            hits = 0

            def do_GET(self) -> None:  # noqa: N802
                type(self).hits += 1
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1/private.pdf")
                self.end_headers()

            def log_message(self, _format, *_args) -> None:
                return

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=httpd.serve_forever, name="test-pdf-redirect", daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{httpd.server_port}/report.pdf"
        checked_urls: list[str] = []

        def policy(target: str, *, resolve: bool = True):
            del resolve
            checked_urls.append(target)
            if target == url:
                return None
            if target.startswith("http://127.0.0.1/private"):
                return "non-public target"
            return None

        try:
            with mock.patch("codey.research.browser_search.check_fetch_url", side_effect=policy):
                result = BrowserSearchProvider().fetch(url)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=1.0)

        self.assertEqual(result["text"], "ERROR: non-public target (after redirect)")
        self.assertEqual(RedirectHandler.hits, 1)
        self.assertEqual(checked_urls, [url, "http://127.0.0.1/private.pdf"])

    def test_browser_search_pdf_sentinel_download_runs_outside_browser_thread(self) -> None:
        provider = BrowserSearchProvider()
        sentinel = {
            "url": "https://example.com/report.pdf",
            "title": "Report PDF",
            "text": "",
            "content_kind": "pdf_download",
            "mime_type": "application/pdf",
            "truncated": False,
        }
        streamed = {
            "url": "https://example.com/report.pdf",
            "title": "Report PDF",
            "text": "downloaded",
            "content_kind": "pdf",
            "mime_type": "application/pdf",
            "truncated": False,
        }

        with (
            mock.patch("codey.research.browser_search.browser_worker.call", return_value=sentinel),
            mock.patch("codey.research.browser_search._download_pdf_streaming", return_value=streamed) as download,
        ):
            result = provider.fetch("https://example.com/report")

        self.assertEqual(result, streamed)
        download.assert_called_once_with("https://example.com/report.pdf", mime_type="application/pdf")

    def test_browser_search_pdf_streaming_checks_cancellation_between_chunks(self) -> None:
        class StreamingResponse:
            status = 200
            headers = {"content-type": "application/pdf"}

            def __init__(self) -> None:
                self.calls = 0

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def geturl(self) -> str:
                return "https://example.com/report.pdf"

            def getcode(self) -> int:
                return 200

            def read(self, size=-1) -> bytes:
                self.calls += 1
                return b"x" * min(size if size > 0 else 8, 8)

        response = StreamingResponse()

        with (
            mock.patch("codey.research.browser_search.check_fetch_url", return_value=None),
            mock.patch("codey.research.browser_search._open_url_no_redirect", return_value=response),
            mock.patch(
                "codey.research.browser_search.cancellation.check",
                side_effect=[None, None, None, cancellation.TaskCancelled("stop")],
            ),
        ):
            with self.assertRaises(cancellation.TaskCancelled):
                BrowserSearchProvider().fetch("https://example.com/report.pdf")

        self.assertEqual(response.calls, 1)


if __name__ == "__main__":
    unittest.main()
