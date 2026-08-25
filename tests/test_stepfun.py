from __future__ import annotations

import unittest
from contextlib import nullcontext
from unittest import mock

from codey.provider_diagnostics import ControlMissing
from codey.provider_submission import SendAttempt, SubmissionUncertain
from codey.providers.web_drivers import stepfun


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

    def _newest_first_page(self, replies: list[str]):
        """Fake page whose DOM order is newest-first: replies[0] is latest.

        ``page.evaluate`` raises, forcing every read through the
        Playwright-locator fallback; ``locator(...).all()`` returns the
        nodes in DOM order.
        """
        page = mock.MagicMock()
        page.evaluate.side_effect = RuntimeError("execution context destroyed")
        nodes = []
        for reply in replies:
            node = mock.Mock()
            node.inner_text.return_value = reply
            nodes.append(node)
        locator = mock.Mock()
        locator.all.return_value = list(nodes)
        page.locator.return_value = locator
        return page

    def test_response_count_fallback_counts_only_visible_nodes(self) -> None:
        # rawCount=4 / filteredCount=2 on the live page: hidden duplicates
        # must not inflate the fallback count used as a baseline. The count
        # reuses the texts ladder, so its last resort is the same locator
        # scan fresh/latest degrade to.
        page = self._newest_first_page(["B", "A"])

        self.assertEqual(stepfun._response_count_fallback(page), 2)
        page.locator.assert_called_once_with(
            f"{stepfun.PROFILE.selector('response')} >> visible=true"
        )

    def test_response_count_fallback_prefers_reason_filtered_texts_js(self) -> None:
        # When the simplified texts JS works, the count derives from it: one
        # filter source for baseline arithmetic and reads, never two that
        # can drift.
        page = self._reason_dom_page(["B", "A"])

        self.assertEqual(stepfun._response_count_fallback(page), 2)
        args = page.evaluate.call_args.args
        self.assertIn(".reason-render-ext", args[0])
        self.assertEqual(args[1], stepfun.PROFILE.selector("response"))
        page.locator.assert_not_called()

    def test_fresh_response_fallback_reads_newest_first_head_slice(self) -> None:
        # Live probe: filtered index 0 holds the NEWEST reply. baseline=1
        # must therefore return B (the head), never A (the tail).
        page = self._newest_first_page(["B", "A"])

        self.assertEqual(stepfun._fresh_response_text(page, 1), "B")

    def _reason_dom_page(self, texts: list[str]):
        """Page where only the string-arg fallback JS works.

        Simulates the realistic degradation: the object-payload evaluates
        fail (serialization), while the simplified string-arg JS applies
        main-path filtering and returns the two real responses newest-first,
        as it would on a [reasonB, B, reasonA, A] DOM.
        """
        page = mock.Mock()

        def evaluate(js, *args):
            if args and isinstance(args[0], dict):
                raise RuntimeError("object payload unsupported")
            return list(texts)

        page.evaluate.side_effect = evaluate
        return page

    def test_fresh_response_fallback_excludes_reason_blocks(self) -> None:
        page = self._reason_dom_page(["B", "A"])

        text = stepfun._fresh_response_text(page, 1)

        self.assertEqual(text, "B")
        args = page.evaluate.call_args.args
        self.assertIn(".reason-render-ext", args[0])
        self.assertEqual(args[1], stepfun.PROFILE.selector("response"))

    def test_latest_response_fallback_excludes_reason_blocks(self) -> None:
        # Degradation ladder for latest: main JS dies -> simplified
        # reason-filtered JS answers -> (locator scan would be last).
        page = self._newest_first_page(["B", "A"])
        seen_js = []

        def evaluate(js, *args):
            seen_js.append(str(js))
            if js == stepfun._LATEST_RESPONSE_TEXT_JS:
                raise RuntimeError("execution context destroyed")
            return ["B", "A"]

        page.evaluate.side_effect = evaluate

        self.assertEqual(stepfun._latest_response_text(page), "B")
        self.assertIn(".reason-render-ext", seen_js[1])
        self.assertEqual(len(seen_js), 2)  # main JS, then the filtered fallback JS

    def test_response_texts_fallback_js_mirrors_main_filtering(self) -> None:
        # The simplified fallback JS must keep every filter of the main-path
        # fresh/latest JS: reason exclusion, visibility, and text trim.
        for token in (
            ".reason-render-ext",
            "getBoundingClientRect",
            "visibility",
            "display",
            "innerText",
        ):
            self.assertIn(token, stepfun._RESPONSE_TEXTS_FALLBACK_JS)

    def test_response_texts_fallback_degrades_to_locator_scan(self) -> None:
        # Even when every evaluate fails, the locator scan still returns the
        # newest-first visible replies (documented residual limit: it cannot
        # exclude reasoning copies without JS).
        page = self._newest_first_page(["B", "A"])

        self.assertEqual(stepfun._response_texts_fallback(page), ("B", "A"))

    def test_degraded_baseline_unit_mismatch_is_pinned_behavior(self) -> None:
        """Known residual limit, pinned consciously.

        Probe case: baseline=2 was captured by the healthy node-count JS;
        reads then degrade to the non-empty texts ladder where only
        ["B", "A"] remain (an empty placeholder vanished). The head slice
        comes out empty instead of returning "B". This is fallback-only,
        never a DOM-direction issue; late-response polling and
        recover_response are the recovery paths. If this test starts
        failing because the units were unified upstream, update it.
        """
        page = self._newest_first_page(["B", "A"])

        assert stepfun._fresh_response_text(page, 2) == ""
        # Same degraded mode, self-consistent count: 2 non-empty texts.
        assert stepfun._response_count_fallback(page) == 2

    def test_latest_response_fallback_takes_first_visible_node(self) -> None:
        page = self._newest_first_page(["B", "A"])

        # The generic tail-first locate_response() would return A here.
        self.assertEqual(stepfun._latest_response_text(page), "B")

    def test_latest_response_fallback_never_uses_generic_locate_response(self) -> None:
        page = self._newest_first_page([])

        with mock.patch.object(stepfun.controls, "locate_response") as locate:
            self.assertEqual(stepfun._latest_response_text(page), "")

        locate.assert_not_called()

    def test_fresh_response_fallback_baseline_exceeding_count_returns_empty(self) -> None:
        page = self._newest_first_page(["B", "A"])

        self.assertEqual(stepfun._fresh_response_text(page, 5), "")

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

    def test_fill_message_until_stable_refills_after_stepfun_clears_draft(self) -> None:
        textarea = mock.Mock()

        with (
            mock.patch.object(stepfun, "_fill_message", side_effect=["hello", "hello"]) as fill,
            mock.patch.object(
                stepfun,
                "_composer_retains_text",
                side_effect=[False, True],
            ),
            mock.patch.object(stepfun.cancellation, "wait") as wait,
        ):
            submitted = stepfun._fill_message_until_stable(textarea, "hello")

        self.assertEqual(submitted, "hello")
        self.assertEqual(fill.call_count, 2)
        wait.assert_called_with(stepfun.COMPOSER_REFILL_DELAY)

    def test_fill_message_until_stable_rejects_repeated_stepfun_hydration_clears(self) -> None:
        textarea = mock.Mock()

        with (
            mock.patch.object(stepfun, "_fill_message", return_value="hello") as fill,
            mock.patch.object(stepfun, "_composer_retains_text", return_value=False),
            mock.patch.object(stepfun.cancellation, "wait"),
        ):
            with self.assertRaisesRegex(ControlMissing, "did not keep"):
                stepfun._fill_message_until_stable(textarea, "hello")

        self.assertEqual(fill.call_count, stepfun.COMPOSER_REFILL_ATTEMPTS)

    def test_composer_retains_text_does_not_require_send_button(self) -> None:
        textarea = mock.Mock()

        with (
            mock.patch.object(
                stepfun.controls,
                "control_has_text",
                side_effect=[True, True, True],
            ) as has_text,
            mock.patch.object(stepfun, "_send_button") as send_button,
            mock.patch.object(stepfun.cancellation, "wait"),
        ):
            retained = stepfun._composer_retains_text(
                textarea,
                "hello",
                settle_time=stepfun.COMPOSER_SETTLE_TICK * 2,
            )

        self.assertTrue(retained)
        self.assertEqual(has_text.call_count, 3)
        send_button.assert_not_called()

    def test_chat_reports_send_button_when_text_stable_but_button_missing(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()

        with (
            mock.patch.object(stepfun, "wait_ready"),
            mock.patch.object(stepfun, "_response_count", return_value=0),
            mock.patch.object(stepfun, "_response_action_count", return_value=0),
            mock.patch.object(stepfun, "_message_box", return_value=textarea),
            mock.patch.object(stepfun, "_fill_message_until_stable", return_value="hello"),
            mock.patch.object(stepfun.send_loop, "response_watch", return_value=nullcontext()),
            mock.patch.object(stepfun, "_send_button", return_value=None),
            mock.patch.object(stepfun.controls, "confirm_control") as confirm,
            mock.patch.object(stepfun.controls, "reject_control") as reject,
        ):
            with self.assertRaisesRegex(ControlMissing, "send button"):
                stepfun.chat(page, "hello", response_timeout=120)

        confirm.assert_called_once_with(stepfun.PROVIDER_ID, stepfun.controls.CONTROL_MESSAGE_BOX)
        reject.assert_called_once_with(
            stepfun.PROVIDER_ID,
            stepfun.controls.CONTROL_SEND_BUTTON,
            page=page,
        )

    def test_wait_response_footer_ready_returns_after_new_stable_action(self) -> None:
        page = mock.Mock()

        with (
            mock.patch.object(stepfun, "_response_action_count", side_effect=[1, 2, 2, 2]),
            mock.patch.object(stepfun.cancellation, "wait") as wait,
        ):
            stepfun._wait_response_footer_ready(page, action_baseline=1, timeout=1.0)

        self.assertEqual(wait.call_count, 3)

    def test_submit_requires_profiled_send_button(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()

        with (
            mock.patch.object(stepfun, "_send_button", return_value=None),
            mock.patch.object(stepfun.controls, "reject_control") as reject,
        ):
            with self.assertRaisesRegex(ControlMissing, "send button"):
                stepfun._submit(page, textarea, baseline=0, submitted_text="hello")

        textarea.press.assert_not_called()
        reject.assert_called_once_with(
            stepfun.PROVIDER_ID,
            stepfun.controls.CONTROL_SEND_BUTTON,
            page=page,
        )

    def test_submit_does_not_double_click_when_first_submit_actually_landed(self) -> None:
        # Regression guard: the confirmation watcher can lose a race with a
        # slow first submit; a second forced click would post twice.
        page = mock.Mock()
        textarea = mock.Mock()
        first_button = mock.Mock()
        retry_button = mock.Mock()

        with (
            mock.patch.object(stepfun, "_send_button", side_effect=[first_button, retry_button]),
            mock.patch.object(stepfun, "_wait_submission_started", return_value=False),
            mock.patch.object(stepfun, "_submission_started", return_value=True),
            mock.patch.object(stepfun.cancellation, "wait"),
            mock.patch.object(stepfun.controls, "confirm_control") as confirm,
        ):
            attempt = stepfun._submit(page, textarea, baseline=0, submitted_text="hello")

        first_button.click.assert_called_once_with()
        retry_button.click.assert_not_called()
        self.assertTrue(attempt.confirmed)
        confirm.assert_called_once_with(stepfun.PROVIDER_ID, stepfun.controls.CONTROL_SEND_BUTTON)

    def test_submit_retries_click_when_first_click_does_not_start_submission(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        first_button = mock.Mock()
        retry_button = mock.Mock()

        with (
            mock.patch.object(stepfun, "_send_button", side_effect=[first_button, retry_button]),
            mock.patch.object(stepfun, "_wait_submission_started", side_effect=[False, True]),
            mock.patch.object(stepfun, "_submission_started", return_value=False),
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
            mock.patch.object(stepfun, "_fill_message_until_stable", return_value="hello"),
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
