"""ResearchSourceGateway: acquisition spine over provider + ledger."""

from __future__ import annotations

import unittest

from codey.research.ledger import ResearchLedger
from codey.research.source_gateway import ResearchSourceGateway


class _FakeSearch:
    def __init__(self, results=(), pages=None, errors=None) -> None:
        self._results = list(results)
        self._pages = dict(pages or {})
        self._errors = dict(errors or {})
        self.name = "fake-search"

    def search(self, query: str, limit: int = 8):
        if "search" in self._errors:
            raise self._errors["search"]
        return list(self._results)[:limit]

    def fetch(self, url: str):
        if url in self._errors:
            raise self._errors[url]
        if url in self._pages:
            return dict(self._pages[url])
        raise ValueError(f"no fixture for {url}")


def _gateway(pages=None, results=(), errors=None):
    failures: list[tuple] = []
    ledger = ResearchLedger()
    gateway = ResearchSourceGateway(
        search_provider=_FakeSearch(results, pages, errors),
        ledger=ledger,
        on_failure=lambda area, action, error, url="": failures.append(
            (area, action, str(error), url)
        ),
    )
    return gateway, ledger, failures


_HTML_PAGE = {
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
        page = dict(_HTML_PAGE, text="ERROR: unsupported content type: video/mp4")
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
    def _opened(self, gateway, url="https://example.com/article"):
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


if __name__ == "__main__":
    unittest.main()
