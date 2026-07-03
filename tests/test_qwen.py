from __future__ import annotations

import unittest
import threading
from unittest import mock

from codey import cancellation, qwen
from codey.provider_submission import SendAttempt


class QwenDriverTests(unittest.TestCase):
    def test_cancelled_chat_exits_before_touching_page(self) -> None:
        event = threading.Event()
        event.set()
        page = mock.Mock()

        with cancellation.scope(event):
            with self.assertRaises(cancellation.TaskCancelled):
                qwen.chat(page, "hello")

        page.locator.assert_not_called()
        page.goto.assert_not_called()

    def test_ready_timeout_allows_slow_homepage(self) -> None:
        self.assertGreaterEqual(qwen.READY_TIMEOUT, 90)

    def test_wait_late_response_requires_generation_completion(self) -> None:
        with (
            mock.patch.object(qwen, "_response_count", return_value=2),
            mock.patch.object(qwen, "_last_text", return_value="final reply"),
            mock.patch.object(qwen, "_generation_complete", side_effect=[False, True]),
            mock.patch.object(qwen, "_final_text", return_value="raw final reply"),
        ):
            reply = qwen._wait_late_response(
                object(),
                baseline=1,
                grace=0.05,
                tick=0,
            )

        self.assertEqual(reply, "raw final reply")

    def test_wait_late_response_accepts_changed_text_without_count_increase(self) -> None:
        with (
            mock.patch.object(qwen, "_response_count", return_value=1),
            mock.patch.object(qwen, "_last_text", return_value="replacement reply"),
            mock.patch.object(qwen, "_generation_complete", return_value=True),
            mock.patch.object(qwen, "_final_text", return_value="raw replacement reply"),
        ):
            reply = qwen._wait_late_response(
                object(),
                baseline=1,
                baseline_text="old reply",
                grace=0.01,
                tick=0,
            )

        self.assertEqual(reply, "raw replacement reply")

    def test_last_text_reads_only_the_latest_answer_node(self) -> None:
        page = mock.Mock()
        response = mock.Mock()
        response.inner_text.return_value = "reply"

        with mock.patch.object(qwen.controls, "locate_response", return_value=response):
            self.assertEqual(qwen._last_text(page), "reply")

        response.inner_text.assert_called_once_with()

    def test_fill_message_commits_inserted_text_to_qwen_composer_state(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()

        submitted_text = qwen._fill_message(page, textarea, "hello")

        self.assertEqual(
            textarea.method_calls,
            [
                mock.call.click(),
                mock.call.press("Control+A"),
                mock.call.press("Backspace"),
                mock.call.press("End"),
                mock.call.press("Space"),
            ],
        )
        textarea.fill.assert_not_called()
        page.keyboard.insert_text.assert_called_once_with("hello")
        self.assertEqual(submitted_text, "hello ")

    def test_submit_confirms_that_qwen_accepted_message(self) -> None:
        page = mock.Mock()
        send = mock.Mock()

        with mock.patch.object(qwen.controls, "visible_locator", return_value=send):
            qwen._submit(page, baseline=0)

        send.click.assert_called_once_with()

    def test_submit_never_clicks_twice_while_waiting_for_confirmation(self) -> None:
        page = mock.Mock()
        send = mock.Mock()

        with (
            mock.patch.object(qwen.controls, "visible_locator", return_value=send),
            mock.patch.object(qwen, "_submission_started", side_effect=[False, False, True]),
            mock.patch.object(qwen.cancellation, "wait"),
        ):
            qwen._submit(page, baseline=0)

        send.click.assert_called_once_with()

    def test_submit_rechecks_state_before_retry_click(self) -> None:
        page = mock.Mock()
        send = mock.Mock()

        with (
            mock.patch.object(qwen, "_send_button", return_value=send),
            mock.patch.object(qwen, "_submission_started", return_value=True),
            mock.patch.object(qwen.time, "time", side_effect=[0, 0]),
            mock.patch.object(qwen.cancellation, "wait"),
        ):
            qwen._submit(page, baseline=0)

        send.click.assert_called_once_with()

    def test_uncertain_submission_never_clicks_twice(self) -> None:
        page = mock.Mock()
        send = mock.Mock()

        with (
            mock.patch.object(qwen, "_send_button", return_value=send),
            mock.patch.object(qwen, "_submission_started", return_value=False),
            mock.patch.object(qwen.time, "time", side_effect=[0, 16]),
            mock.patch.object(qwen.cancellation, "wait"),
        ):
            attempt = qwen._submit(page, baseline=0, submitted_text="hello")

        send.click.assert_called_once_with()
        self.assertEqual(attempt.phase, "attempted")

    def test_uncertain_submission_continues_until_delayed_answer(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        attempt = SendAttempt()
        attempt.submit("click", lambda: None)
        with (
            mock.patch.object(qwen, "wait_ready"),
            mock.patch.object(qwen, "_message_box", return_value=textarea),
            mock.patch.object(qwen, "_fill_message"),
            mock.patch.object(qwen, "_submit", return_value=attempt),
            mock.patch.object(qwen, "_response_count", side_effect=[0, 1]),
            mock.patch.object(qwen, "_last_text", return_value="delayed reply"),
            mock.patch.object(qwen, "_empty_response_visible", return_value=False),
            mock.patch.object(qwen, "_generation_complete", return_value=True),
            mock.patch.object(qwen, "_final_text", return_value="raw delayed reply"),
            mock.patch.object(qwen.controls, "control_has_text", return_value=True),
            mock.patch.object(qwen.controls, "confirm_control"),
            mock.patch.object(qwen.cancellation, "wait"),
        ):
            reply = qwen.chat(
                page, "hello", response_timeout=1, stable_ticks=0, tick=0, min_wait=0
            )

        self.assertEqual(reply, "raw delayed reply")
        self.assertTrue(attempt.confirmed)

    def test_submission_started_requires_generation_or_new_response(self) -> None:
        with (
            mock.patch.object(qwen.controls, "visible_locator", return_value=None),
            mock.patch.object(qwen, "_response_count", return_value=0),
        ):
            started = qwen._submission_started(mock.Mock(), baseline=0)

        self.assertFalse(started)

    def test_submission_probe_failure_remains_uncertain(self) -> None:
        with mock.patch.object(qwen, "_visible_locator", side_effect=ValueError("detached")):
            self.assertFalse(qwen._submission_started(mock.Mock(), baseline=0, submitted_text="hello"))

    def test_submission_started_accepts_new_response(self) -> None:
        with (
            mock.patch.object(qwen.controls, "visible_locator", return_value=None),
            mock.patch.object(qwen, "_response_count", return_value=2),
        ):
            started = qwen._submission_started(mock.Mock(), baseline=1)

        self.assertTrue(started)

    def test_copy_last_text_returns_raw_source_and_restores_clipboard(self) -> None:
        page = mock.Mock()
        messages = mock.Mock()
        message = mock.Mock()
        copy_locator = mock.Mock()
        copy_button = mock.Mock()
        page.locator.return_value = messages
        messages.count.return_value = 1
        messages.last = message
        message.locator.return_value = copy_locator
        copy_locator.last = copy_button
        copy_button.count.return_value = 1
        copy_button.is_visible.return_value = True

        with mock.patch.object(
            qwen,
            "copy_action_text",
            return_value='{"tool":"done","args":{"summary":"raw"}}',
        ) as copy_action:
            raw = qwen._copy_last_text(page)

        self.assertEqual(raw, '{"tool":"done","args":{"summary":"raw"}}')
        copy_action.assert_called_once_with(page, copy_button, origin=qwen.QWEN_URL)

    def test_regenerate_empty_response_uses_last_response_action(self) -> None:
        page = mock.Mock()
        responses = mock.Mock()
        response = mock.Mock()
        regenerate_locator = mock.Mock()
        regenerate = mock.Mock()
        page.locator.return_value = responses
        responses.count.return_value = 1
        responses.last = response
        response.locator.return_value = regenerate_locator
        regenerate_locator.last = regenerate
        regenerate.count.return_value = 1
        regenerate.is_visible.return_value = True

        with mock.patch.object(qwen, "_visible_locator", return_value=mock.Mock()):
            recovered = qwen._regenerate_empty_response(page)

        self.assertTrue(recovered)
        response.locator.assert_called_once_with(qwen.REGENERATE)
        regenerate.click.assert_called_once_with()

    def test_chat_regenerates_one_empty_response(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        with (
            mock.patch.object(qwen, "wait_ready"),
            mock.patch.object(qwen, "_message_box", return_value=textarea),
            mock.patch.object(qwen, "_submit"),
            mock.patch.object(qwen, "_response_count", side_effect=[0, 1]),
            mock.patch.object(qwen, "_last_text", return_value="recovered"),
            mock.patch.object(qwen, "_empty_response_visible", side_effect=[True, False]),
            mock.patch.object(qwen, "_generation_complete", return_value=True),
            mock.patch.object(qwen, "_regenerate_empty_response", return_value=True) as regenerate,
            mock.patch.object(qwen, "_final_text", return_value="raw recovered"),
            mock.patch.object(qwen.cancellation, "wait"),
        ):
            reply = qwen.chat(
                page,
                "hello",
                response_timeout=1,
                stable_ticks=0,
                tick=0,
                min_wait=0,
            )

        self.assertEqual(reply, "raw recovered")
        regenerate.assert_called_once_with(page)

    def test_resolve_preference_selects_first_visible_reply(self) -> None:
        page = mock.Mock()
        choices = mock.Mock()
        choice = mock.Mock()
        page.locator.return_value = choices
        choices.count.return_value = 2
        choices.nth.side_effect = [choice, mock.Mock()]
        choice.is_visible.return_value = True

        with mock.patch.object(qwen, "_visible_locator", return_value=None):
            resolved = qwen._resolve_preference(page)

        self.assertTrue(resolved)
        choices.nth.assert_called_once_with(0)
        choice.click.assert_called_once_with()

    def test_final_text_resolves_preference_before_copying(self) -> None:
        calls = []
        with (
            mock.patch.object(
                qwen,
                "_resolve_preference",
                side_effect=lambda page: calls.append("preference"),
            ),
            mock.patch.object(
                qwen,
                "_copy_last_text",
                side_effect=lambda page: calls.append("copy") or "raw reply",
            ),
        ):
            result = qwen._final_text(object())

        self.assertEqual(result, "raw reply")
        self.assertEqual(calls, ["preference", "copy"])

    def test_final_text_rejects_missing_raw_response(self) -> None:
        with (
            mock.patch.object(qwen, "_resolve_preference", return_value=False),
            mock.patch.object(qwen, "_copy_last_text", return_value=""),
            mock.patch.object(qwen, "_last_text", return_value=""),
        ):
            with self.assertRaisesRegex(RuntimeError, "Qwen Studio response"):
                qwen._final_text(object())


if __name__ == "__main__":
    unittest.main()
