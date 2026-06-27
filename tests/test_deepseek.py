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
            mock.patch.object(deepseek, "_final_text", return_value="raw late reply"),
        ):
            self.assertEqual(
                deepseek._wait_late_response(object(), baseline=1, grace=0.01, tick=0),
                "raw late reply",
            )

    def test_wait_late_response_accepts_changed_last_text_without_count_increase(self) -> None:
        with (
            mock.patch.object(deepseek, "_response_count", return_value=2),
            mock.patch.object(deepseek, "_last_text", return_value="replacement reply"),
            mock.patch.object(deepseek, "_final_text", return_value="raw replacement reply"),
        ):
            self.assertEqual(
                deepseek._wait_late_response(
                    object(),
                    baseline=2,
                    baseline_text="previous reply",
                    grace=0.01,
                    tick=0,
                ),
                "raw replacement reply",
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

    def test_copy_last_text_uses_first_answer_action(self) -> None:
        page = mock.Mock()
        responses = mock.Mock()
        response = mock.Mock()
        container = mock.Mock()
        actions = mock.Mock()
        copy_button = mock.Mock()
        page.locator.return_value = responses
        responses.count.return_value = 1
        responses.last = response
        response.locator.return_value = container
        container.locator.return_value = actions
        actions.count.return_value = 5
        actions.first = copy_button
        copy_button.is_visible.return_value = True

        with mock.patch.object(
            deepseek,
            "copy_action_text",
            return_value="<codey>raw</codey>",
        ) as copy_action:
            raw = deepseek._copy_last_text(page)

        self.assertEqual(raw, "<codey>raw</codey>")
        response.locator.assert_called_once_with("xpath=../..")
        copy_action.assert_called_once_with(page, copy_button, origin=deepseek.DEEPSEEK_URL)


if __name__ == "__main__":
    unittest.main()
