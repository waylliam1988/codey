"""Research URL single owner: keys, parsing, and open-table fallback."""

from __future__ import annotations

import unittest

from codey.research.urls import (
    canonical_key,
    full_key,
    host_key,
    opened_url,
    parsed_url,
)


class _Ledger:
    def __init__(self, pairs: dict[str, str]) -> None:
        self._pairs = dict(pairs)

    def canonical_opened_url(self, url: str) -> str:
        return self._pairs.get(str(url or "").strip(), "")


class ResearchUrlsTests(unittest.TestCase):
    def test_parsed_url_never_raises(self) -> None:
        self.assertEqual(parsed_url("https://example.com/x").hostname, "example.com")
        self.assertEqual(parsed_url("https://[bad").hostname, None)
        self.assertEqual(parsed_url("").path, "")
        self.assertEqual(parsed_url(None).scheme, "")

    def test_canonical_key_strips_www_and_slash(self) -> None:
        self.assertEqual(
            canonical_key("https://www.example.com/a/"),
            "https://example.com/a",
        )
        self.assertEqual(
            canonical_key("HTTPS://EXAMPLE.COM"),
            "https://example.com",
        )

    def test_full_key_keeps_query(self) -> None:
        self.assertEqual(
            full_key("https://www.example.com/a/?x=1"),
            "https://example.com/a?x=1",
        )
        self.assertEqual(full_key("not a url"), "://not a url")

    def test_host_key_normalizes(self) -> None:
        self.assertEqual(host_key("https://WWW.Example.COM/x"), "example.com")
        self.assertEqual(host_key("garbage"), "")

    def test_opened_url_prefers_canonical_then_stripped_raw(self) -> None:
        ledger = _Ledger({"https://a.example/r": "https://a.example/final"})
        self.assertEqual(
            opened_url(ledger, "https://a.example/r"), "https://a.example/final"
        )
        self.assertEqual(
            opened_url(ledger, "  https://b.example/x  "), "https://b.example/x"
        )
        self.assertEqual(opened_url(ledger, ""), "")
        self.assertEqual(opened_url(ledger, None), "")

    def test_opened_url_survives_broken_ledger(self) -> None:
        class _Broken:
            def canonical_opened_url(self, url: str) -> str:
                raise ValueError("bad table")

        self.assertEqual(
            opened_url(_Broken(), "https://b.example/x"), "https://b.example/x"
        )


if __name__ == "__main__":
    unittest.main()
