from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from codey.provider_diagnostics import capture_provider_failure


class ProviderDiagnosticsTests(unittest.TestCase):
    def test_capture_provider_failure_keeps_only_small_page_context(self) -> None:
        page = SimpleNamespace(url="https://chat.example/c", title=lambda: "Example Chat")
        now = datetime(2026, 6, 28, 1, 2, 3, tzinfo=timezone.utc)

        failure = capture_provider_failure(
            model="MiMo",
            action="send",
            page=page,
            error=TimeoutError("response timed out"),
            now=now,
        )

        self.assertEqual(failure.to_dict(), {
            "model": "MiMo",
            "action": "send",
            "url": "https://chat.example/c",
            "title": "Example Chat",
            "message": "response timed out",
            "time": "2026-06-28T01:02:03+00:00",
        })

    def test_capture_provider_failure_tolerates_broken_page_accessors(self) -> None:
        class BrokenPage:
            @property
            def url(self) -> str:
                raise RuntimeError("url unavailable")

            def title(self) -> str:
                raise RuntimeError("title unavailable")

        failure = capture_provider_failure(
            model="Qwen",
            action="read_response",
            page=BrokenPage(),
            error=RuntimeError("selector missing"),
        )

        self.assertEqual(failure.url, "")
        self.assertEqual(failure.title, "")
        self.assertEqual(failure.message, "selector missing")


if __name__ == "__main__":
    unittest.main()
