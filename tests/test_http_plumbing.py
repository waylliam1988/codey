from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codey import __version__
from codey.app import http_plumbing


class _Handler:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}
        self.status = 0
        self.sent_headers: dict[str, str] = {}
        self.wfile = io.BytesIO()

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, key: str, value: str) -> None:
        self.sent_headers[key] = value

    def end_headers(self) -> None:
        pass


class HttpPlumbingTests(unittest.TestCase):
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
        self.assertEqual(first.wfile.getvalue(), b"console.log('one');")
        self.assertEqual(second.status, 304)
        self.assertEqual(second.sent_headers["Content-Length"], "0")
        self.assertEqual(second.wfile.getvalue(), b"")
        self.assertEqual(third.status, 200)
        self.assertNotEqual(third.sent_headers["ETag"], etag)
        self.assertEqual(third.wfile.getvalue(), b"console.log('changed');")

    def test_send_index_caches_rendered_version(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            web_dir = Path(td)
            (web_dir / "index.html").write_text(
                "<html>__CODEY_VERSION__</html>",
                encoding="utf-8",
            )
            with mock.patch.object(http_plumbing, "WEB_DIR", web_dir):
                first = _Handler()
                http_plumbing.send_index(first)
                etag = first.sent_headers["ETag"]
                second = _Handler({"If-None-Match": etag})
                http_plumbing.send_index(second)

        self.assertEqual(first.status, 200)
        self.assertIn(__version__.encode("utf-8"), first.wfile.getvalue())
        self.assertNotIn(b"__CODEY_VERSION__", first.wfile.getvalue())
        self.assertEqual(second.status, 304)
        self.assertEqual(second.wfile.getvalue(), b"")


if __name__ == "__main__":
    unittest.main()
