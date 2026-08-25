from __future__ import annotations

import unittest
import threading
import time
from unittest import mock

from codey import cancellation
from codey.provider_submission import SendAttempt
from codey.providers.web_drivers import deepseek


class DeepSeekTimeoutTests(unittest.TestCase):
    def test_cancelled_chat_exits_before_touching_page(self) -> None:
        event = threading.Event()
        event.set()
        page = mock.Mock()

        with cancellation.scope(event):
            with self.assertRaises(cancellation.TaskCancelled):
                deepseek.chat(page, "hello")

        page.locator.assert_not_called()
        page.goto.assert_not_called()

    def test_provider_wait_observes_stop_within_one_second(self) -> None:
        event = threading.Event()
        timer = threading.Timer(0.1, event.set)
        started = time.monotonic()
        timer.start()
        try:
            with (
                cancellation.scope(event),
                mock.patch.object(deepseek, "_message_box", return_value=None),
            ):
                with self.assertRaises(cancellation.TaskCancelled):
                    deepseek.wait_ready(object(), timeout=30)
        finally:
            timer.cancel()

        self.assertLess(time.monotonic() - started, 1.0)

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
        response = mock.Mock()
        container = mock.Mock()
        actions = mock.Mock()
        copy_button = mock.Mock()
        response.locator.return_value = container
        container.locator.return_value = actions
        actions.count.return_value = 5
        actions.first = copy_button
        copy_button.is_visible.return_value = True

        with (
            mock.patch.object(deepseek.controls, "locate_response", return_value=response),
            mock.patch.object(
                deepseek,
                "copy_action_text",
                return_value='{"tool":"done","args":{"summary":"raw"}}',
            ) as copy_action,
        ):
            raw = deepseek._copy_last_text(page)

        self.assertEqual(raw, '{"tool":"done","args":{"summary":"raw"}}')
        response.locator.assert_called_once_with("xpath=../..")
        copy_action.assert_called_once_with(page, copy_button, origin=deepseek.DEEPSEEK_URL)

    def test_final_text_prefers_dom_json_when_copy_is_stale_prompt(self) -> None:
        with (
            mock.patch.object(deepseek, "_copy_last_text", return_value="Your previous reply did not contain a valid JSON tool call."),
            mock.patch.object(
                deepseek,
                "_last_text",
                return_value='{"tool":"done","args":{"summary":"ok"}}',
            ),
            mock.patch.object(deepseek.controls, "confirm_control") as confirm_control,
        ):
            raw = deepseek._final_text(object())

        self.assertEqual(raw, '{"tool":"done","args":{"summary":"ok"}}')
        confirm_control.assert_called_once_with(deepseek.PROVIDER_ID, deepseek.controls.CONTROL_RESPONSE)

    def test_final_text_keeps_non_json_copy_for_normal_answers(self) -> None:
        with (
            mock.patch.object(deepseek, "_copy_last_text", return_value="normal prose"),
            mock.patch.object(deepseek, "_last_text", return_value="normal prose"),
            mock.patch.object(deepseek.controls, "confirm_control"),
        ):
            raw = deepseek._final_text(object())

        self.assertEqual(raw, "normal prose")

    def test_final_text_repairs_missing_trailing_json_tool_brace(self) -> None:
        incomplete = '{"tool":"done","args":{"summary":"ok"}'
        complete = '{"tool":"done","args":{"summary":"ok"}}'
        with (
            mock.patch.object(deepseek, "_copy_last_text", return_value=incomplete),
            mock.patch.object(deepseek, "_last_text", return_value=incomplete),
            mock.patch.object(deepseek.controls, "confirm_control"),
        ):
            raw = deepseek._final_text(object())

        self.assertEqual(raw, complete)

    def test_uncertain_submission_uses_only_the_chosen_action(self) -> None:
        page = mock.Mock()
        message_box = mock.Mock()
        button = mock.Mock()
        with (
            mock.patch.object(deepseek, "_send_button", return_value=button),
            mock.patch.object(deepseek, "_wait_submission_started", return_value=False),
        ):
            attempt = deepseek._submit(page, message_box, 0, "", "hello")

        button.click.assert_called_once_with()
        message_box.press.assert_not_called()
        self.assertEqual(attempt.phase, "attempted")

    def test_uncertain_submission_continues_until_delayed_answer(self) -> None:
        page = mock.Mock()
        message_box = mock.Mock()
        attempt = SendAttempt()
        attempt.submit("click", lambda: None)
        with (
            mock.patch.object(deepseek, "wait_ready"),
            mock.patch.object(deepseek, "_message_box", return_value=message_box),
            mock.patch.object(deepseek, "_submit", return_value=attempt),
            mock.patch.object(deepseek, "_response_count", side_effect=[0, 0, 1, 1, 1]),
            mock.patch.object(deepseek, "_rate_limit_visible", return_value=False),
            mock.patch.object(deepseek, "_last_text", return_value="delayed reply"),
            mock.patch.object(deepseek, "_final_text", return_value="raw delayed reply"),
            mock.patch.object(deepseek.controls, "control_has_text", return_value=True),
            mock.patch.object(deepseek.controls, "confirm_control"),
            mock.patch.object(deepseek.controls, "recover_flow") as recover_flow,
            mock.patch.object(deepseek.cancellation, "wait"),
        ):
            reply = deepseek.chat(
                page, "hello", response_timeout=1, stable_ticks=1, tick=0, min_wait=0
            )

        self.assertEqual(reply, "raw delayed reply")
        self.assertTrue(attempt.confirmed)
        recover_flow.assert_not_called()

    def test_chat_stops_response_watch_once_after_driver_error(self) -> None:
        page = mock.Mock()
        message_box = mock.Mock()
        message_box.click.side_effect = RuntimeError("click failed")
        with (
            mock.patch.object(deepseek, "wait_ready"),
            mock.patch.object(deepseek, "_message_box", return_value=message_box),
            mock.patch.object(deepseek, "_response_count", return_value=0),
            mock.patch.object(deepseek.controls, "start_response_watch") as start_watch,
            mock.patch.object(deepseek.controls, "stop_response_watch") as stop_watch,
            mock.patch.object(deepseek.controls, "reject_control"),
        ):
            with self.assertRaisesRegex(RuntimeError, "click failed"):
                deepseek.chat(page, "hello", response_timeout=1, tick=0)

        start_watch.assert_called_once_with(page, deepseek.PROVIDER_ID)
        stop_watch.assert_called_once_with(page, deepseek.PROVIDER_ID)

    def test_chat_returns_stable_json_before_general_stable_ticks(self) -> None:
        page = mock.Mock()
        message_box = mock.Mock()
        attempt = SendAttempt()
        attempt.submit("click", lambda: None)
        json_reply = '{"tool":"done","args":{"summary":"ok"}}'
        with (
            mock.patch.object(deepseek, "wait_ready"),
            mock.patch.object(deepseek, "_message_box", return_value=message_box),
            mock.patch.object(deepseek, "_submit", return_value=attempt),
            mock.patch.object(deepseek, "_response_count", side_effect=[0, *([1] * 20)]),
            mock.patch.object(deepseek, "_rate_limit_visible", return_value=False),
            mock.patch.object(deepseek, "_last_text", return_value=json_reply),
            mock.patch.object(deepseek, "_final_text", return_value=json_reply),
            mock.patch.object(deepseek.controls, "control_has_text", return_value=True),
            mock.patch.object(deepseek.controls, "confirm_control"),
            mock.patch.object(deepseek.controls, "flow_stage_ready", return_value=False) as flow_ready,
            mock.patch.object(deepseek.cancellation, "wait"),
        ):
            reply = deepseek.chat(
                page,
                "hello",
                response_timeout=1,
                stable_ticks=99,
                tick=0,
                min_wait=0,
            )

        self.assertEqual(reply, json_reply)
        self.assertLess(flow_ready.call_count, 3)

    def test_chat_does_not_finish_on_stable_incomplete_json_tool_reply(self) -> None:
        page = mock.Mock()
        message_box = mock.Mock()
        attempt = SendAttempt()
        attempt.submit("click", lambda: None)
        incomplete = '{"tool":"done","args":{"summary":"still streaming"}'
        complete = '{"tool":"done","args":{"summary":"complete"}}'
        with (
            mock.patch.object(deepseek, "wait_ready"),
            mock.patch.object(deepseek, "_message_box", return_value=message_box),
            mock.patch.object(deepseek, "_submit", return_value=attempt),
            mock.patch.object(deepseek, "_response_count", side_effect=[0, *([1] * 30)]),
            mock.patch.object(deepseek, "_rate_limit_visible", return_value=False),
            mock.patch.object(
                deepseek,
                "_last_text",
                side_effect=[incomplete, incomplete, incomplete, complete, complete, complete],
            ),
            mock.patch.object(deepseek, "_final_text", return_value=complete),
            mock.patch.object(deepseek.controls, "control_has_text", return_value=True),
            mock.patch.object(deepseek.controls, "confirm_control"),
            mock.patch.object(deepseek.controls, "flow_stage_ready", return_value=False),
            mock.patch.object(deepseek.cancellation, "wait"),
        ):
            reply = deepseek.chat(
                page,
                "hello",
                response_timeout=1,
                stable_ticks=99,
                tick=0,
                min_wait=0,
            )

        self.assertEqual(reply, complete)

    def test_chat_repairs_stable_missing_trailing_brace_after_general_stability(self) -> None:
        page = mock.Mock()
        message_box = mock.Mock()
        attempt = SendAttempt()
        attempt.submit("click", lambda: None)
        incomplete = '{"tool":"done","args":{"summary":"ok"}'
        complete = '{"tool":"done","args":{"summary":"ok"}}'
        with (
            mock.patch.object(deepseek, "wait_ready"),
            mock.patch.object(deepseek, "_message_box", return_value=message_box),
            mock.patch.object(deepseek, "_submit", return_value=attempt),
            mock.patch.object(deepseek, "_response_count", side_effect=[0, *([1] * 30)]),
            mock.patch.object(deepseek, "_rate_limit_visible", return_value=False),
            mock.patch.object(deepseek, "_last_text", return_value=incomplete),
            mock.patch.object(deepseek, "_final_text", return_value=complete),
            mock.patch.object(deepseek.controls, "control_has_text", return_value=True),
            mock.patch.object(deepseek.controls, "confirm_control"),
            mock.patch.object(deepseek.controls, "flow_stage_ready", return_value=False),
            mock.patch.object(deepseek.cancellation, "wait"),
        ):
            reply = deepseek.chat(
                page,
                "hello",
                response_timeout=1,
                stable_ticks=4,
                tick=0,
                min_wait=0,
            )

        self.assertEqual(reply, complete)

    def test_chat_returns_stable_malformed_json_tool_reply_for_protocol_repair(self) -> None:
        page = mock.Mock()
        message_box = mock.Mock()
        attempt = SendAttempt()
        attempt.submit("click", lambda: None)
        malformed = '{"tool":"web_search","args":{"query":""Omega paper" validation method PDF"}}'
        with (
            mock.patch.object(deepseek, "wait_ready"),
            mock.patch.object(deepseek, "_message_box", return_value=message_box),
            mock.patch.object(deepseek, "_submit", return_value=attempt),
            mock.patch.object(deepseek, "_response_count", side_effect=[0, *([1] * 30)]),
            mock.patch.object(deepseek, "_rate_limit_visible", return_value=False),
            mock.patch.object(deepseek, "_last_text", return_value=malformed),
            mock.patch.object(deepseek, "_final_text", return_value=malformed),
            mock.patch.object(deepseek.controls, "control_has_text", return_value=True),
            mock.patch.object(deepseek.controls, "confirm_control"),
            mock.patch.object(deepseek.controls, "flow_stage_ready", return_value=True),
            mock.patch.object(deepseek.cancellation, "wait"),
        ):
            reply = deepseek.chat(
                page,
                "hello",
                response_timeout=1,
                stable_ticks=3,
                tick=0,
                min_wait=0,
            )

        self.assertEqual(reply, malformed)

    def test_rate_limit_visible_checks_deepseek_warning_text(self) -> None:
        page = mock.Mock()
        body = mock.Mock()
        page.locator.return_value = body
        body.inner_text.return_value = "消息发送过于频繁，请稍后重试"

        self.assertTrue(deepseek._rate_limit_visible(page))
        page.locator.assert_called_once_with("body")
        body.inner_text.assert_called_once_with(timeout=1000)

    def test_click_rate_limit_retry_uses_latest_visible_warning_button(self) -> None:
        page = mock.Mock()
        buttons = mock.Mock()
        hidden = mock.Mock()
        visible = mock.Mock()
        page.locator.return_value = buttons
        buttons.count.return_value = 2
        buttons.nth.side_effect = [visible, hidden]
        visible.is_visible.return_value = True
        visible.inner_text.return_value = "重试"

        with mock.patch.object(deepseek.cancellation, "wait") as wait:
            self.assertTrue(deepseek._click_rate_limit_retry(page))

        wait.assert_called_once_with(deepseek.RATE_LIMIT_COOLDOWN)
        page.locator.assert_called_once_with(deepseek.RATE_LIMIT_RETRY_BUTTON)
        buttons.nth.assert_called_once_with(1)
        visible.click.assert_called_once_with()
        hidden.click.assert_not_called()

    def test_click_rate_limit_retry_ignores_other_warning_action(self) -> None:
        page = mock.Mock()
        buttons = mock.Mock()
        warning = mock.Mock()
        page.locator.return_value = buttons
        buttons.count.return_value = 1
        buttons.nth.return_value = warning
        warning.is_visible.return_value = True
        warning.inner_text.return_value = "确认"

        with mock.patch.object(deepseek.cancellation, "wait"):
            self.assertFalse(deepseek._click_rate_limit_retry(page))

        warning.click.assert_not_called()

    def test_chat_retries_repeated_rate_limits_and_waits_for_answer(self) -> None:
        page = mock.Mock()
        message_box = mock.Mock()
        attempt = SendAttempt()
        attempt.submit("click", lambda: None)

        with (
            mock.patch.object(deepseek, "wait_ready"),
            mock.patch.object(deepseek, "_message_box", return_value=message_box),
            mock.patch.object(deepseek, "_submit", return_value=attempt),
            mock.patch.object(
                deepseek,
                "_response_count",
                side_effect=[0, 0, 0, 1, 1, 1, 1],
            ),
            mock.patch.object(deepseek, "_rate_limit_visible", return_value=True),
            mock.patch.object(deepseek, "_click_rate_limit_retry", return_value=True) as retry,
            mock.patch.object(deepseek, "_last_text", return_value="reply after retry"),
            mock.patch.object(deepseek, "_final_text", return_value="raw reply after retry"),
            mock.patch.object(deepseek.controls, "control_has_text", return_value=True),
            mock.patch.object(deepseek.controls, "confirm_control"),
            mock.patch.object(deepseek.cancellation, "wait"),
        ):
            reply = deepseek.chat(
                page, "hello", response_timeout=1, stable_ticks=0, tick=0, min_wait=0
            )

        self.assertEqual(reply, "raw reply after retry")
        self.assertEqual(retry.call_count, 2)
        retry.assert_has_calls([mock.call(page), mock.call(page)])
        self.assertTrue(attempt.confirmed)


if __name__ == "__main__":
    unittest.main()
