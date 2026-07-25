from __future__ import annotations

import unittest
from unittest import mock

from codey import stepfun
from codey.provider_submission import SendAttempt


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


if __name__ == "__main__":
    unittest.main()
