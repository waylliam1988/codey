from __future__ import annotations

import unittest
import threading
from unittest import mock

from codey import cancellation, mimo


class MimoDriverTests(unittest.TestCase):
    def test_cancelled_chat_exits_before_touching_page(self) -> None:
        event = threading.Event()
        event.set()
        page = mock.Mock()

        with cancellation.scope(event):
            with self.assertRaises(cancellation.TaskCancelled):
                mimo.chat(page, "hello")

        page.locator.assert_not_called()
        page.goto.assert_not_called()

    def test_ready_timeout_allows_slow_homepage(self) -> None:
        self.assertGreaterEqual(mimo.READY_TIMEOUT, 90)

    def test_dismisses_only_profiled_announcement_close_button(self) -> None:
        page = mock.Mock()
        button = mock.Mock()
        with mock.patch.object(mimo, "_visible_locator", side_effect=[button, None]) as visible:
            dismissed = mimo._dismiss_known_notice(page)

        self.assertTrue(dismissed)
        visible.assert_has_calls([
            mock.call(page, mimo.PROFILE.selector("dismiss_notice")),
            mock.call(page, mimo.PROFILE.selector("dismiss_notice")),
        ])
        button.click.assert_called_once_with()

    def test_last_text_reads_latest_markdown_answer(self) -> None:
        page = mock.Mock()
        response = mock.Mock()
        response.inner_text.return_value = "reply"

        with mock.patch.object(mimo.controls, "locate_response", return_value=response):
            self.assertEqual(mimo._last_text(page), "reply")

        response.inner_text.assert_called_once_with()

    def test_copy_last_text_uses_first_action_after_latest_answer(self) -> None:
        page = mock.Mock()
        responses = mock.Mock()
        response = mock.Mock()
        buttons = mock.Mock()
        before_button = mock.Mock()
        copy_button = mock.Mock()

        page.locator.return_value = buttons
        response.bounding_box.return_value = {"x": 280, "y": 100, "width": 600, "height": 80}
        buttons.count.return_value = 2
        buttons.nth.side_effect = [before_button, copy_button]
        before_button.is_visible.return_value = True
        before_button.bounding_box.return_value = {"x": 270, "y": 120, "width": 28, "height": 28}
        copy_button.is_visible.return_value = True
        copy_button.bounding_box.return_value = {"x": 282, "y": 174, "width": 28, "height": 28}

        with (
            mock.patch.object(mimo.controls, "locate_response", return_value=response),
            mock.patch.object(mimo, "copy_action_text", return_value="raw reply") as copy_action,
        ):
            raw = mimo._copy_last_text(page)

        self.assertEqual(raw, "raw reply")
        response.scroll_into_view_if_needed.assert_called_once()
        copy_action.assert_called_once_with(page, copy_button, origin=mimo.MIMO_ORIGIN)

    def test_submit_presses_enter_and_waits_for_new_response(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()

        with (
            mock.patch.object(mimo.controls, "visible_locator", return_value=textarea),
            mock.patch.object(mimo, "_wait_submission_started", return_value=True),
            mock.patch.object(mimo, "_send_button", return_value=None) as send_button,
        ):
            mimo._submit(page, baseline=0)

        textarea.press.assert_called_once_with("Enter")
        send_button.assert_called_once_with(page)

    def test_submit_clicks_explicit_send_button_when_available(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        button = mock.Mock()

        with (
            mock.patch.object(mimo.controls, "visible_locator", return_value=textarea),
            mock.patch.object(mimo, "_wait_submission_started", return_value=True) as wait_started,
            mock.patch.object(mimo, "_send_button", return_value=button),
        ):
            mimo._submit(page, baseline=0, submitted_text="hello")

        textarea.press.assert_not_called()
        button.click.assert_called_once_with()
        wait_started.assert_called_once_with(page, 0, "", "hello")

    def test_send_button_uses_mimo_send_selector_not_nearby_upload_button(self) -> None:
        page = mock.Mock()
        send = mock.Mock()
        send.is_visible.return_value = True
        send.is_enabled.return_value = True
        locator = mock.Mock()
        locator.count.return_value = 1
        locator.nth.return_value = send
        page.locator.return_value = locator

        result = mimo._send_button(page)

        self.assertIs(result, send)
        page.locator.assert_any_call(mimo.PROFILE.selector("send_button"))

    def test_submission_started_accepts_cleared_input_after_send_click(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        textarea.input_value.return_value = ""

        with (
            mock.patch.object(mimo, "_response_count", return_value=0),
            mock.patch.object(mimo, "_last_text", return_value=""),
            mock.patch.object(mimo.controls, "visible_locator", return_value=textarea),
        ):
            started = mimo._submission_started(page, baseline=0, submitted_text="hello")

        self.assertTrue(started)

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
