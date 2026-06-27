from __future__ import annotations

import unittest
from unittest import mock

from codey import deepseek


class DeepSeekTimeoutTests(unittest.TestCase):
    def test_ready_timeout_allows_slow_deepseek_homepage(self) -> None:
        self.assertGreaterEqual(deepseek.READY_TIMEOUT, 90)

    def test_wait_late_response_returns_text_after_baseline(self) -> None:
        with (
            mock.patch.object(deepseek, "_response_count", return_value=2),
            mock.patch.object(deepseek, "_last_text", return_value="late reply"),
        ):
            self.assertEqual(deepseek._wait_late_response(object(), baseline=1, grace=0.01, tick=0), "late reply")

    def test_wait_late_response_accepts_changed_last_text_without_count_increase(self) -> None:
        with (
            mock.patch.object(deepseek, "_response_count", return_value=2),
            mock.patch.object(deepseek, "_last_text", return_value="replacement reply"),
        ):
            self.assertEqual(
                deepseek._wait_late_response(
                    object(),
                    baseline=2,
                    baseline_text="previous reply",
                    grace=0.01,
                    tick=0,
                ),
                "replacement reply",
            )

    def test_wait_late_response_returns_empty_without_new_message(self) -> None:
        with (
            mock.patch.object(deepseek, "_response_count", return_value=1),
            mock.patch.object(deepseek, "_last_text", return_value="old reply"),
        ):
            self.assertEqual(
                deepseek._wait_late_response(
                    object(),
                    baseline=1,
                    baseline_text="old reply",
                    grace=0.01,
                    tick=0,
                ),
                "",
            )


if __name__ == "__main__":
    unittest.main()
