from __future__ import annotations

import unittest
from unittest import mock

from codey import mimo


class MimoDriverTests(unittest.TestCase):
    def test_ready_timeout_allows_slow_homepage(self) -> None:
        self.assertGreaterEqual(mimo.READY_TIMEOUT, 90)

    def test_last_text_reads_latest_markdown_answer(self) -> None:
        page = mock.Mock()
        page.evaluate.return_value = "reply"

        self.assertEqual(mimo._last_text(page), "reply")

        script = page.evaluate.call_args.args[0]
        self.assertIn("answers[answers.length - 1]", script)

    def test_copy_last_text_uses_first_action_after_latest_answer(self) -> None:
        page = mock.Mock()
        responses = mock.Mock()
        response = mock.Mock()
        buttons = mock.Mock()
        before_button = mock.Mock()
        copy_button = mock.Mock()

        page.locator.side_effect = lambda selector: responses if selector == mimo.ANSWER else buttons
        responses.count.return_value = 1
        responses.last = response
        response.bounding_box.return_value = {"x": 280, "y": 100, "width": 600, "height": 80}
        buttons.count.return_value = 2
        buttons.nth.side_effect = [before_button, copy_button]
        before_button.is_visible.return_value = True
        before_button.bounding_box.return_value = {"x": 270, "y": 120, "width": 28, "height": 28}
        copy_button.is_visible.return_value = True
        copy_button.bounding_box.return_value = {"x": 282, "y": 174, "width": 28, "height": 28}

        with mock.patch.object(mimo, "copy_action_text", return_value="raw reply") as copy_action:
            raw = mimo._copy_last_text(page)

        self.assertEqual(raw, "raw reply")
        response.scroll_into_view_if_needed.assert_called_once()
        copy_action.assert_called_once_with(page, copy_button, origin=mimo.MIMO_ORIGIN)

    def test_submit_presses_enter_and_waits_for_new_response(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()

        with (
            mock.patch.object(mimo, "_visible_locator", return_value=textarea),
            mock.patch.object(mimo, "_submission_started", return_value=True),
        ):
            mimo._submit(page, baseline=0)

        textarea.press.assert_called_once_with("Enter")

    def test_final_text_falls_back_to_visible_answer(self) -> None:
        with (
            mock.patch.object(mimo, "_copy_last_text", return_value=""),
            mock.patch.object(mimo, "_last_text", return_value='{"verdict":"approved"}'),
        ):
            self.assertEqual(mimo._final_text(object()), '{"verdict":"approved"}')

    def test_final_text_rejects_missing_raw_and_visible_response(self) -> None:
        with (
            mock.patch.object(mimo, "_copy_last_text", return_value=""),
            mock.patch.object(mimo, "_last_text", return_value=""),
        ):
            with self.assertRaisesRegex(RuntimeError, "raw Xiaomi MiMo response"):
                mimo._final_text(object())


if __name__ == "__main__":
    unittest.main()
