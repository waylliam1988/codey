"""ResearchSourceGateway: acquisition spine over provider + ledger."""

from __future__ import annotations

import unittest
from collections.abc import Mapping, Sequence

import pytest

from codey.policies.network import check_fetch_url
from codey.research.ledger import ResearchLedger
from codey.research.source_document import PublicationMetadata
from codey.research.source_gateway import ResearchSourceGateway
from codey.research.source_observation import source_observation
from codey.runtime.core.cancellation import TaskCancelled
from codey.types.json_value import JsonObject, decode_json_object

pytestmark = pytest.mark.usefixtures("public_source_dns")


class _FakeSearch:
    def __init__(self, results: Sequence[JsonObject] = (), pages: Mapping[str, Mapping[str, object]] | None = None,
                 errors: Mapping[str, BaseException] | None = None) -> None:
        self._results = list(results)
        self._pages = dict(pages or {})
        self._errors = dict(errors or {})
        self.name = "fake-search"

    def search(self, query: str, limit: int = 8) -> list[JsonObject]:
        if "search" in self._errors:
            raise self._errors["search"]
        return list(self._results)[:limit]

    def fetch(self, url: str) -> dict[str, object]:
        if url in self._errors:
            raise self._errors[url]
        if url in self._pages:
            return dict(self._pages[url])
        raise ValueError(f"no fixture for {url}")


def _gateway(pages: Mapping[str, Mapping[str, object]] | None = None, results: Sequence[JsonObject] = (),
             errors: Mapping[str, BaseException] | None = None
             ) -> tuple[ResearchSourceGateway, ResearchLedger, list[tuple[str, str, str, str]]]:
    failures: list[tuple[str, str, str, str]] = []
    ledger = ResearchLedger()
    gateway = ResearchSourceGateway(
        search_provider=_FakeSearch(results, pages, errors),
        ledger=ledger,
        on_failure=lambda area, action, error, url="": failures.append(
            (area, action, str(error), url)
        ),
    )
    return gateway, ledger, failures


_HTML_PAGE = {"status": "ok",
    "url": "https://example.com/article",
    "title": "Example",
    "text": "Evidence text about aluminum supply.",
    "mime_type": "text/html",
}


class GatewaySearchTests(unittest.TestCase):
    def test_search_records_and_returns_hits(self) -> None:
        gateway, ledger, failures = _gateway(
            results=[{"title": "T", "url": "https://example.com/a", "snippet": "s"}]
        )
        outcome = gateway.search("aluminum", 8)
        self.assertEqual(outcome.error, "")
        self.assertEqual(len(outcome.hits), 1)
        self.assertEqual(len(ledger.searches), 1)
        self.assertEqual(failures, [])

    def test_search_failure_reports_error_and_records(self) -> None:
        gateway, _ledger, failures = _gateway(errors={"search": RuntimeError("down")})
        outcome = gateway.search("aluminum", 8)
        self.assertIn("search failed", outcome.error)
        self.assertEqual(outcome.hits, ())
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][:2], ("search", "search"))

    def test_search_empty_query_is_rejected(self) -> None:
        gateway, ledger, _failures = _gateway()
        outcome = gateway.search("   ", 8)
        self.assertNotEqual(outcome.error, "")
        self.assertEqual(len(ledger.searches), 0)


class GatewayOpenTests(unittest.TestCase):
    def test_direct_and_discovered_urls_both_use_configured_provider(self) -> None:
        from unittest import mock

        home = "https://example.com/"
        article = "https://example.com/article"
        gateway, ledger, failures = _gateway(pages={home: dict(_HTML_PAGE, url=home), article: _HTML_PAGE})
        gateway.direct_urls = frozenset({home})
        with mock.patch("codey.research.source_download._download_text_fallback",
                        side_effect=AssertionError("gateway bypassed its provider")):
            assert gateway.open(home).status == "ok"
            assert gateway.open(article).status == "ok"
        assert len(ledger.opened_sources) == 2 and failures == []

    def test_open_abstract_preserves_scope_in_ledger(self) -> None:
        page = dict(_HTML_PAGE, content_kind="abstract", mime_type="application/xml")
        gateway, ledger, _failures = _gateway(pages={page["url"]: page})
        outcome = gateway.open(page["url"])
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.document.content_kind, "abstract")
        self.assertEqual(ledger.opened_sources[0].content_kind, "abstract")
        self.assertEqual(outcome.document.mime_type, "application/xml")

    def test_open_html_records_and_reports_read_urls(self) -> None:
        gateway, ledger, failures = _gateway(pages={"https://example.com/article": _HTML_PAGE})
        outcome = gateway.open("https://example.com/article")
        self.assertEqual(outcome.status, "ok")
        self.assertIsNotNone(outcome.document)
        self.assertEqual(outcome.read_urls, ("https://example.com/article",))
        self.assertEqual(len(ledger.opened_sources), 1)
        self.assertEqual(failures, [])

    def test_open_redirect_records_both_urls(self) -> None:
        page = dict(_HTML_PAGE, url="https://example.com/final")
        gateway, ledger, _failures = _gateway(pages={"https://example.com/article": page})
        outcome = gateway.open("https://example.com/article")
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(
            outcome.read_urls, ("https://example.com/article", "https://example.com/final")
        )
        self.assertEqual(ledger.opened_sources[0].final_url, "https://example.com/final")

    def test_open_denied_and_fetch_errors_map_status(self) -> None:
        gateway, _ledger, failures = _gateway()
        denied = gateway.open("https://example.com/landing")
        # Landing pages are skipped by selection policy, not fetched.
        self.assertIn(denied.status, ("skipped", "error"))
        failed = gateway.open(
            "https://example.com/article",
        )
        # No fixture: fetch raises ValueError -> error outcome + failure record.
        self.assertEqual(failed.status, "error")
        self.assertIn("open failed", failed.detail)
        self.assertTrue(any(item[0] == "browser" for item in failures))

    def test_open_unsupported_type_is_skipped(self) -> None:
        page = dict(_HTML_PAGE, status="skipped", detail="unsupported content type: video/mp4",
                    text="SKIPPED: unsupported content type: video/mp4")
        gateway, _ledger, _failures = _gateway(pages={"https://example.com/v": page})
        outcome = gateway.open("https://example.com/v")
        self.assertEqual(outcome.status, "skipped")
        self.assertIn("unsupported content type", outcome.detail)

    def test_redirect_policy_refusal_records_failure(self) -> None:
        from unittest import mock

        import codey.research.source_gateway as gateway_module

        page = dict(_HTML_PAGE, url="https://example.com/final")
        gateway, _ledger, failures = _gateway(
            pages={"https://example.com/article": page}
        )
        with mock.patch.object(
            gateway_module,
            "check_fetch_url",
            side_effect=[None, "refusing final host"],
        ):
            outcome = gateway.open("https://example.com/article")
        self.assertEqual(outcome.status, "error")
        self.assertIn("after redirect", outcome.detail)
        self.assertEqual(len(failures), 1)
        area, action, detail, url = failures[0]
        self.assertEqual((area, action), ("browser", "open"))
        self.assertIn("after redirect", detail)
        self.assertEqual(url, "https://example.com/final")


class GatewaySearchInsideTests(unittest.TestCase):
    def _opened(self, gateway: ResearchSourceGateway, url: str = "https://example.com/article") -> str:
        outcome = gateway.open(url)
        self.assertEqual(outcome.status, "ok")
        return outcome

    def test_needs_open_and_unknown_source(self) -> None:
        gateway, _ledger, _failures = _gateway(pages={"https://example.com/article": _HTML_PAGE})
        missing = gateway.search_inside("https://example.com/other", "aluminum", 6)
        self.assertEqual(missing.status, "needs_open")
        self.assertIn("open the source", missing.detail)

    def test_html_search_inside_finds_text(self) -> None:
        gateway, _ledger, _failures = _gateway(pages={"https://example.com/article": _HTML_PAGE})
        self._opened(gateway)
        outcome = gateway.search_inside("https://example.com/article", "aluminum", 6)
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.final_url, "https://example.com/article")
        self.assertTrue(outcome.hits)
        self.assertTrue(all("aluminum" in hit.snippet.lower() for hit in outcome.hits))


@pytest.mark.parametrize("case", ["publisher", "wrong_identity", "all_unavailable", "cancelled"])
def test_full_text_candidates_keep_attempts_and_original_work_identity(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def check_fixture_url(url: str, *, use_cache: bool = True) -> str | None:
        return check_fetch_url(url, resolve=False, use_cache=use_cache)

    monkeypatch.setattr("codey.research.source_gateway.check_fetch_url", check_fixture_url)
    url = "https://pubmed.ncbi.nlm.nih.gov/16466392/"
    archive = "https://pmc.ncbi.nlm.nih.gov/articles/PMC123456/"
    resolver = "https://doi.org/10.1234/atlas"
    landing = "https://example.com/article/atlas"
    title = "Atlas test report"
    article = (title + "\nDOI: 10.1234/atlas\n# Methods\n" + "We measured the inputs and outputs ten times. " * 20
               + "\n# Results\n" + "The median input changed after calibration. " * 12)
    metadata = PublicationMetadata("10.1234/atlas", "PMC123456", "", "preprint", "2")
    pages: dict[str, dict[str, object]] = {
        url: {"status": "ok", "url": url, "title": title, "text": "This abstract describes an Atlas comparison.",
              "content_kind": "abstract", "publication": metadata},
        archive: {"status": "error", "url": archive, "detail": "archive temporarily unavailable",
                  "failure_code": "page_unavailable"},
        resolver: {"status": "ok", "url": landing, "title": title, "text": article, "content_kind": "html"},
    }
    errors: dict[str, BaseException] = {}
    if case == "wrong_identity":
        pages[resolver]["text"] = article.replace("10.1234/atlas", "10.1234/another")
    if case == "all_unavailable":
        errors[resolver] = OSError("unavailable")
    if case == "cancelled":
        errors[archive] = TaskCancelled()
    gateway, ledger, _failures = _gateway(pages=pages, errors=errors)

    if case == "cancelled":
        with pytest.raises(TaskCancelled):
            gateway.open(url)
        assert not ledger.opened_sources
        return
    result = gateway.open(url)
    assert result.status == "ok" and result.document is not None
    assert len(ledger.opened_sources) == 1
    attempts = result.acquisition["attempts"]
    assert isinstance(attempts, list)
    acquisitions = [decode_json_object(row) for row in attempts]
    assert [row["requested_url"] for row in acquisitions] == [url, archive, resolver]
    if case == "publisher":
        assert result.document.content_kind == "html" and result.document.final_url == landing
        assert result.document.text == article
        assert acquisitions[2]["status"] == "ok"
        assert result.document.publication == PublicationMetadata("10.1234/atlas", "PMC123456")
    else:
        assert result.document.content_kind == "abstract" and result.document.final_url == url
        assert result.document.publication == metadata
        assert acquisitions[2]["status"] == "error"
    recovered, body, _ = source_observation(result.observation)
    assert recovered.publication == result.document.publication and body == result.document.text


def test_legacy_publication_payload_still_decodes_and_roundtrips() -> None:
    old = {"doi": "10.1234/atlas", "pmcid": "PMC123456", "status": "retracted"}
    assert PublicationMetadata.decode(old).to_payload() == old


if __name__ == "__main__":
    unittest.main()
