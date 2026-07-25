from __future__ import annotations

import unittest
from contextlib import nullcontext
from unittest import mock

from codey import stepfun
from codey.provider_submission import SendAttempt, SubmissionUncertain


class StepFunDriverTests(unittest.TestCase):
    def test_wait_ready_accepts_visible_message_box(self) -> None:
        page = mock.Mock()
        box = mock.Mock()

        with mock.patch.object(stepfun, "_message_box", return_value=box):
            stepfun.wait_ready(page, timeout=0)

    def test_new_chat_navigates_to_stepfun_url(self) -> None:
        page = mock.Mock()

        with mock.patch.object(stepfun, "wait_ready") as wait_ready:
            stepfun.new_chat(page, timeout=7.5)

        page.goto.assert_called_once()
        self.assertEqual(page.goto.call_args.args[0], stepfun.STEPFUN_URL)
        wait_ready.assert_called_once()

    def test_profile_targets_current_icon_send_button(self) -> None:
        self.assertIn(
            "button:has(svg.custom-icon-send-outline)",
            stepfun.PROFILE.selectors("send_button"),
        )

    def test_profile_targets_response_footer_reload_button(self) -> None:
        self.assertIn(
            "button:has(svg.custom-icon-reload-outline)",
            stepfun.PROFILE.selectors("response_action"),
        )

    def test_fresh_response_text_passes_selector_and_baseline_as_one_payload(self) -> None:
        page = mock.Mock()
        page.evaluate.return_value = "fresh reply"

        text = stepfun._fresh_response_text(page, 3)

        self.assertEqual(text, "fresh reply")
        self.assertEqual(page.evaluate.call_args.args[0], stepfun._FRESH_RESPONSE_TEXT_JS)
        self.assertEqual(
            page.evaluate.call_args.args[1],
            {"selector": stepfun.PROFILE.selector("response"), "baseline": 3},
        )

    def test_final_text_does_not_return_old_response_after_baseline(self) -> None:
        page = mock.Mock()

        with (
            mock.patch.object(stepfun, "_fresh_response_text", return_value=""),
            mock.patch.object(stepfun, "_latest_response_text", return_value="old reply"),
            mock.patch.object(stepfun.controls, "reject_control") as reject,
        ):
            with self.assertRaisesRegex(RuntimeError, "StepFun response"):
                stepfun._final_text(page, baseline=2)

        reject.assert_called_once_with(stepfun.PROVIDER_ID, stepfun.controls.CONTROL_RESPONSE)

    def test_final_text_can_use_latest_when_no_previous_response_exists(self) -> None:
        page = mock.Mock()

        with (
            mock.patch.object(stepfun, "_fresh_response_text", return_value=""),
            mock.patch.object(stepfun, "_latest_response_text", return_value='{"tool":"done","args":{}'),
            mock.patch.object(stepfun.controls, "confirm_control") as confirm,
        ):
            text = stepfun._final_text(page, baseline=0)

        self.assertEqual(text, '{"tool":"done","args":{}}')
        confirm.assert_called_once_with(stepfun.PROVIDER_ID, stepfun.controls.CONTROL_RESPONSE)

    def test_submission_started_does_not_treat_newline_insert_as_submit(self) -> None:
        page = mock.Mock()
        box = mock.Mock()
        box.input_value.return_value = "hello\n"

        with (
            mock.patch.object(stepfun, "_response_count", return_value=0),
            mock.patch.object(stepfun, "_fresh_response_text", return_value=""),
            mock.patch.object(stepfun, "_message_box", return_value=box),
            mock.patch.object(stepfun.controls, "flow_matches", return_value=False) as flow_matches,
        ):
            started = stepfun._submission_started(page, baseline=0, submitted_text="hello")

        self.assertFalse(started)
        flow_matches.assert_called_once()

    def test_submission_started_accepts_empty_composer_as_submit(self) -> None:
        page = mock.Mock()
        box = mock.Mock()
        box.input_value.return_value = ""

        with (
            mock.patch.object(stepfun, "_response_count", return_value=0),
            mock.patch.object(stepfun, "_fresh_response_text", return_value=""),
            mock.patch.object(stepfun, "_message_box", return_value=box),
            mock.patch.object(stepfun.controls, "flow_matches") as flow_matches,
        ):
            started = stepfun._submission_started(page, baseline=0, submitted_text="hello")

        self.assertTrue(started)
        flow_matches.assert_not_called()

    def test_wait_response_footer_ready_returns_after_new_stable_action(self) -> None:
        page = mock.Mock()

        with (
            mock.patch.object(stepfun, "_response_action_count", side_effect=[1, 2, 2, 2]),
            mock.patch.object(stepfun.cancellation, "wait") as wait,
        ):
            stepfun._wait_response_footer_ready(page, action_baseline=1, timeout=1.0)

        self.assertEqual(wait.call_count, 3)

    def test_submit_uses_enter_when_no_profiled_send_button_is_visible(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()

        with (
            mock.patch.object(stepfun, "_send_button", return_value=None),
            mock.patch.object(stepfun, "_wait_submission_started", return_value=True),
            mock.patch.object(stepfun.controls, "reject_control") as reject,
            mock.patch.object(stepfun.controls, "confirm_control") as confirm,
        ):
            attempt = stepfun._submit(page, textarea, baseline=0, submitted_text="hello")

        textarea.press.assert_called_once_with("Enter")
        self.assertIsInstance(attempt, SendAttempt)
        self.assertTrue(attempt.confirmed)
        reject.assert_called_once_with(
            stepfun.PROVIDER_ID,
            stepfun.controls.CONTROL_SEND_BUTTON,
            page=page,
        )
        confirm.assert_not_called()

    def test_submit_retries_click_when_first_click_does_not_start_submission(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        first_button = mock.Mock()
        retry_button = mock.Mock()

        with (
            mock.patch.object(stepfun, "_send_button", side_effect=[first_button, retry_button]),
            mock.patch.object(stepfun, "_wait_submission_started", side_effect=[False, True]),
            mock.patch.object(stepfun.cancellation, "wait") as wait,
            mock.patch.object(stepfun.controls, "reject_control") as reject,
            mock.patch.object(stepfun.controls, "confirm_control") as confirm,
        ):
            attempt = stepfun._submit(page, textarea, baseline=0, submitted_text="hello")

        first_button.click.assert_called_once_with()
        retry_button.click.assert_called_once_with(force=True)
        textarea.press.assert_not_called()
        wait.assert_called_once_with(0.6)
        self.assertEqual(attempt.method, "click")
        self.assertTrue(attempt.confirmed)
        reject.assert_not_called()
        confirm.assert_called_once_with(stepfun.PROVIDER_ID, stepfun.controls.CONTROL_SEND_BUTTON)

    def test_submit_does_not_press_enter_when_click_starts_submission(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        button = mock.Mock()

        with (
            mock.patch.object(stepfun, "_send_button", return_value=button),
            mock.patch.object(stepfun, "_wait_submission_started", return_value=True),
            mock.patch.object(stepfun.controls, "reject_control") as reject,
            mock.patch.object(stepfun.controls, "confirm_control") as confirm,
        ):
            attempt = stepfun._submit(page, textarea, baseline=0, submitted_text="hello")

        button.click.assert_called_once_with()
        textarea.press.assert_not_called()
        self.assertEqual(attempt.method, "click")
        self.assertTrue(attempt.confirmed)
        reject.assert_not_called()
        confirm.assert_called_once_with(stepfun.PROVIDER_ID, stepfun.controls.CONTROL_SEND_BUTTON)

    def test_chat_fails_fast_when_submission_is_not_confirmed(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        attempt = SendAttempt(phase="attempted", method="click")

        with (
            mock.patch.object(stepfun, "wait_ready"),
            mock.patch.object(stepfun, "_response_count", return_value=0),
            mock.patch.object(stepfun, "_message_box", return_value=textarea),
            mock.patch.object(stepfun, "_fill_message"),
            mock.patch.object(stepfun.controls, "control_has_text", return_value=True),
            mock.patch.object(stepfun.controls, "confirm_control"),
            mock.patch.object(stepfun.send_loop, "response_watch", return_value=nullcontext()),
            mock.patch.object(stepfun, "_submit", return_value=attempt),
            mock.patch.object(stepfun.cancellation, "wait"),
            mock.patch.object(stepfun.controls, "reject_control") as reject,
        ):
            with self.assertRaises(SubmissionUncertain):
                stepfun.chat(page, "hello", response_timeout=120)

        reject.assert_not_called()


if __name__ == "__main__":
    unittest.main()
