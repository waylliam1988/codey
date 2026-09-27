"""Packaging regression: web/icon.ico must ship in the wheel.

server.py serves GET /icon.ico from codey/web/icon.ico and the desktop
bootstrap passes it as the window icon. pyproject package-data previously
listed only HTML/CSS/JS, so installed wheels 404'd on /icon.ico.
"""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WebIconPackagingTests(unittest.TestCase):
    def test_package_data_includes_icon(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        package_data = pyproject.get("tool", {}).get("setuptools", {}).get("package-data", {})
        codey_data = package_data.get("codey", [])
        self.assertIn("web/icon.ico", list(codey_data))

    def test_icon_file_exists_in_source_tree(self) -> None:
        self.assertTrue((ROOT / "codey" / "web" / "icon.ico").is_file())

    def test_icon_served_with_image_mime(self) -> None:
        import http.client
        import threading

        from codey.app import server

        httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        host, port = httpd.server_address
        try:
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/icon.ico")
            response = conn.getresponse()
            body = response.read()
            ctype = response.getheader("Content-Type") or ""
            conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

        self.assertEqual(response.status, 200)
        self.assertIn("image/x-icon", ctype)
        self.assertTrue(len(body) > 0)


if __name__ == "__main__":
    unittest.main()
