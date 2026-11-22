from __future__ import annotations

import io
import tempfile
import unittest
from http.client import HTTPMessage
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from codey import __version__
from codey.app import http_plumbing


class _Handler(BaseHTTPRequestHandler):
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = HTTPMessage()
        for key, value in (headers or {}).items():
            self.headers[key] = value
        self.status = 0
        self.sent_headers: dict[str, str] = {}
        self.output = io.BytesIO()
        self.wfile = self.output

    def send_response(self, code: int, message: str | None = None) -> None:
        self.status = code

    def send_header(self, keyword: str, value: str) -> None:
        self.sent_headers[keyword] = value

    def end_headers(self) -> None:
        pass


class _BoundServer(HTTPServer):
    def __init__(self, address: tuple[str, int]) -> None:
        self.server_address = address


class HttpPlumbingTests(unittest.TestCase):
    def test_asset_edit_changes_index_urls_and_invalidates_index_etag(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            web_dir = Path(td)
            assets = web_dir / "assets"
            assets.mkdir()
            css = assets / "app.css"
            css.write_text("body { color: gray; }", encoding="utf-8")
            (web_dir / "index.html").write_text(
                '<link href="/assets/app.css?v=__APP_VERSION__">', encoding="utf-8"
            )
            with mock.patch.object(http_plumbing, "WEB_DIR", web_dir):
                first = _Handler()
                http_plumbing.send_index(first)
                css.write_text("body { color: white; }", encoding="utf-8")
                second = _Handler({"If-None-Match": first.sent_headers["ETag"]})
                http_plumbing.send_index(second)
            self.assertEqual(second.status, 200)
            self.assertNotEqual(first.output.getvalue(), second.output.getvalue())
            self.assertNotEqual(first.sent_headers["ETag"], second.sent_headers["ETag"])

    def test_send_file_uses_etag_revalidation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "asset.js"
            path.write_text("console.log('one');", encoding="utf-8")
            first = _Handler()

            http_plumbing.send_file(first, path, "application/javascript")
            etag = first.sent_headers["ETag"]
            second = _Handler({"If-None-Match": etag})
            http_plumbing.send_file(second, path, "application/javascript")
            path.write_text("console.log('changed');", encoding="utf-8")
            third = _Handler({"If-None-Match": etag})
            http_plumbing.send_file(third, path, "application/javascript")

        self.assertEqual(first.status, 200)
        self.assertEqual(first.sent_headers["Cache-Control"], "no-cache")
        self.assertEqual(first.output.getvalue(), b"console.log('one');")
        self.assertEqual(second.status, 304)
        self.assertEqual(second.sent_headers["Cache-Control"], "no-cache")
        self.assertEqual(second.sent_headers["Content-Length"], "0")
        self.assertEqual(second.output.getvalue(), b"")
        self.assertEqual(third.status, 200)
        self.assertNotEqual(third.sent_headers["ETag"], etag)
        self.assertEqual(third.output.getvalue(), b"console.log('changed');")

    def test_if_none_match_compares_opaque_tags_without_weakness(self) -> None:
        # RFC 9110 section 13.1.2 requires weak comparison for If-None-Match.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "asset.js"
            path.write_text("console.log('one');", encoding="utf-8")
            first = _Handler()
            http_plumbing.send_file(first, path, "application/javascript")
            etag = first.sent_headers["ETag"]
            self.assertTrue(etag.startswith('W/"'))
            second = _Handler({"If-None-Match": etag.removeprefix("W/")})
            http_plumbing.send_file(second, path, "application/javascript")

        self.assertEqual(second.status, 304)
        self.assertEqual(second.sent_headers["ETag"], etag)
        self.assertEqual(second.output.getvalue(), b"")

    def test_if_none_match_accepts_a_comma_in_an_opaque_tag(self) -> None:
        # RFC 9110 section 8.8.3 permits commas inside an opaque entity tag.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "asset,part.js"
            path.write_text("console.log('one');", encoding="utf-8")
            first = _Handler()
            http_plumbing.send_file(first, path, "application/javascript")
            etag = first.sent_headers["ETag"]
            second = _Handler({"If-None-Match": etag})
            http_plumbing.send_file(second, path, "application/javascript")

        self.assertEqual(second.status, 304)
        self.assertEqual(second.output.getvalue(), b"")

    def test_loopback_origin_bindings(self) -> None:
        for bind_ip in ("127.0.0.1", "::1", "0.0.0.0", "::", "192.0.2.1"):
            with self.subTest(bind_ip=bind_ip):
                # Only the public bound address participates; no listener is needed.
                listener = _BoundServer((bind_ip, 8765))
                handler = _Handler({"Host": "localhost:8765"})
                handler.server = listener
                self.assertTrue(http_plumbing.request_origin_allowed(handler))
                handler.headers["Origin"] = "http://localhost:8765"
                self.assertTrue(http_plumbing.request_origin_allowed(handler))
                handler.headers.replace_header("Origin", "http://localhost:8766")
                self.assertFalse(http_plumbing.request_origin_allowed(handler))
                handler.headers.replace_header("Origin", "http://evil.example:8765")
                self.assertFalse(http_plumbing.request_origin_allowed(handler))
                handler.headers.replace_header("Origin", "http://localhost:8765")
                handler.headers.replace_header("Host", "evil.example:8765")
                self.assertFalse(http_plumbing.request_origin_allowed(handler))

    def test_if_none_match_lists_wildcard_and_distinct_tags(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "asset.js"
            path.write_text("console.log('one');", encoding="utf-8")
            first = _Handler()
            http_plumbing.send_file(first, path, "application/javascript")
            etag = first.sent_headers["ETag"]
            strong = etag.removeprefix("W/")
            cases = [
                (etag, 304), (strong, 304), ("*", 304), (f'"different,tag", {strong}', 304),
                (f' W/"different" , {etag} ', 304), ('"different"', 200), ('W/"different"', 200),
                ("W/*", 200), ("", 200), ('"unclosed', 200),
            ]
            for header, status in cases:
                with self.subTest(header=header):
                    handler = _Handler({"If-None-Match": header})
                    http_plumbing.send_file(handler, path, "application/javascript")
                    self.assertEqual(handler.status, status)
                    self.assertEqual(handler.output.getvalue(), b"" if status == 304 else first.output.getvalue())

    def test_send_file_immutable_only_when_version_pinned(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "asset.js"
            path.write_text("console.log('one');", encoding="utf-8")
            pinned = _Handler()
            http_plumbing.send_file(
                pinned, path, "application/javascript", immutable=True
            )
            etag = pinned.sent_headers["ETag"]
            renotified = _Handler({"If-None-Match": etag})
            http_plumbing.send_file(
                renotified, path, "application/javascript", immutable=True
            )

        self.assertEqual(
            pinned.sent_headers["Cache-Control"], "public, max-age=31536000, immutable"
        )
        self.assertEqual(renotified.status, 304)
        self.assertEqual(
            renotified.sent_headers["Cache-Control"], "public, max-age=31536000, immutable"
        )

    def test_send_index_caches_rendered_version(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            web_dir = Path(td)
            (web_dir / "index.html").write_text(
                "<html>__APP_VERSION__</html>",
                encoding="utf-8",
            )
            with mock.patch.object(http_plumbing, "WEB_DIR", web_dir):
                first = _Handler()
                http_plumbing.send_index(first)
                etag = first.sent_headers["ETag"]
                second = _Handler({"If-None-Match": etag})
                http_plumbing.send_index(second)

        self.assertEqual(first.status, 200)
        self.assertEqual(first.sent_headers["Cache-Control"], "no-cache")
        self.assertIn(__version__.encode("utf-8"), first.output.getvalue())
        self.assertNotIn(b"__APP_VERSION__", first.output.getvalue())
        self.assertEqual(second.status, 304)
        self.assertEqual(second.sent_headers["Cache-Control"], "no-cache")
        self.assertEqual(second.output.getvalue(), b"")


if __name__ == "__main__":
    unittest.main()
