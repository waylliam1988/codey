from __future__ import annotations

import threading
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey import cancellation, mimo
from codey.provider_submission import SendAttempt


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

    def test_wait_ready_does_not_accept_page_while_generating(self) -> None:
        page = mock.Mock()

        with (
            mock.patch.object(mimo, "_dismiss_known_notice"),
            mock.patch.object(mimo, "_message_box", return_value=mock.Mock()),
            mock.patch.object(mimo, "_generation_active", return_value=True),
            mock.patch.object(mimo.cancellation, "wait"),
        ):
            with self.assertRaisesRegex(TimeoutError, "input did not appear"):
                mimo.wait_ready(page, timeout=0)

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
        button.click.assert_called_once_with(timeout=2000)

    def test_dismiss_notice_falls_back_to_dom_click_when_outside_viewport(self) -> None:
        page = mock.Mock()
        button = mock.Mock()
        button.click.side_effect = RuntimeError("outside viewport")
        with mock.patch.object(mimo, "_visible_locator", side_effect=[button, None]):
            dismissed = mimo._dismiss_known_notice(page)

        self.assertTrue(dismissed)
        button.click.assert_called_once_with(timeout=2000)
        button.evaluate.assert_called_once_with("el => el.click()")

    def test_dismiss_notice_failure_is_non_blocking(self) -> None:
        page = mock.Mock()
        button = mock.Mock()
        button.click.side_effect = RuntimeError("outside viewport")
        button.evaluate.side_effect = RuntimeError("detached")
        with mock.patch.object(mimo, "_visible_locator", return_value=button):
            dismissed = mimo._dismiss_known_notice(page)

        self.assertFalse(dismissed)

    def test_last_text_reads_latest_markdown_answer(self) -> None:
        page = mock.Mock()
        response = mock.Mock()
        response.evaluate.return_value = "reply"

        with mock.patch.object(mimo.controls, "locate_response", return_value=response):
            self.assertEqual(mimo._last_text(page), "reply")

        response.evaluate.assert_called_once_with(mimo._RESPONSE_TEXT_JS)

    def test_last_text_removes_mimo_thinking_summary(self) -> None:
        page = mock.Mock()
        response = mock.Mock()
        response.evaluate.return_value = '{"tool":"done","args":{"summary":"ok"}}'

        with mock.patch.object(mimo.controls, "locate_response", return_value=response):
            self.assertEqual(mimo._last_text(page), '{"tool":"done","args":{"summary":"ok"}}')

    def test_last_text_rejects_thinking_when_dom_extract_fails(self) -> None:
        page = mock.Mock()
        response = mock.Mock()
        response.evaluate.side_effect = RuntimeError("detached")
        response.inner_text.return_value = "已深度思考（用时 68.4 秒）\n\nLet me analyze..."

        with mock.patch.object(mimo.controls, "locate_response", return_value=response):
            self.assertEqual(mimo._last_text(page), "")

    def test_copy_last_text_uses_first_action_after_latest_answer(self) -> None:
        page = mock.Mock()
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
            mock.patch.object(mimo, "_generation_complete", return_value=True),
            mock.patch.object(mimo.controls, "locate_response", return_value=response),
            mock.patch.object(mimo, "_response_text", return_value="raw reply"),
            mock.patch.object(mimo, "copy_action_text", return_value="raw reply") as copy_action,
        ):
            raw = mimo._copy_last_text(page)

        self.assertEqual(raw, "raw reply")
        response.scroll_into_view_if_needed.assert_called_once()
        page.locator.assert_called_once_with(mimo.COPY_BUTTON)
        copy_action.assert_called_once_with(page, copy_button, origin=mimo.MIMO_ORIGIN)

    def test_copy_last_text_returns_visible_answer_when_copy_includes_thinking(self) -> None:
        page = mock.Mock()
        response = mock.Mock()
        buttons = mock.Mock()
        copy_button = mock.Mock()

        page.locator.return_value = buttons
        response.bounding_box.return_value = {"x": 280, "y": 100, "width": 600, "height": 80}
        buttons.count.return_value = 1
        buttons.nth.return_value = copy_button
        copy_button.is_visible.return_value = True
        copy_button.bounding_box.return_value = {"x": 282, "y": 174, "width": 28, "height": 28}

        with (
            mock.patch.object(mimo, "_generation_complete", return_value=True),
            mock.patch.object(mimo.controls, "locate_response", return_value=response),
            mock.patch.object(mimo, "_response_text", return_value='{"tool":"done","args":{"summary":"ok"}}'),
            mock.patch.object(
                mimo,
                "copy_action_text",
                return_value='已深度思考（用时 1 秒）\n\n{"tool":"done","args":{"summary":"ok"}}',
            ),
        ):
            raw = mimo._copy_last_text(page)

        self.assertEqual(raw, '{"tool":"done","args":{"summary":"ok"}}')

    def test_copy_last_text_waits_for_generation_completion(self) -> None:
        page = mock.Mock()

        with mock.patch.object(mimo, "_generation_complete", return_value=False):
            raw = mimo._copy_last_text(page)

        self.assertEqual(raw, "")
        page.locator.assert_not_called()

    def test_copy_button_lookup_ignores_upload_and_stop_buttons(self) -> None:
        page = mock.Mock()
        response = mock.Mock()
        buttons = mock.Mock()
        response.bounding_box.return_value = {"x": 280, "y": 100, "width": 600, "height": 80}
        buttons.count.return_value = 0
        page.locator.return_value = buttons

        result = mimo._copy_button_after_response(page, response)

        self.assertIsNone(result)
        page.locator.assert_called_once_with(mimo.COPY_BUTTON)

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
        send_button.assert_called_once_with(page, teach=True)

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
        send.evaluate.return_value = {
            "disabled": False,
            "ariaDisabled": "",
            "trackId": "home_send_btn",
            "trackName": "home_send_message",
            "text": "",
            "aria": "",
            "title": "",
            "viewBoxes": ["0 0 19 16"],
            "rect": {
                "left": 10,
                "right": 38,
                "top": 10,
                "bottom": 38,
                "width": 28,
                "height": 28,
                "viewportWidth": 1000,
                "viewportHeight": 800,
            },
        }
        locator = mock.Mock()
        locator.count.return_value = 1
        locator.nth.return_value = send
        page.locator.return_value = locator

        result = mimo._send_button(page)

        self.assertIs(result, send)
        page.locator.assert_any_call(mimo.PROFILE.selector("send_button"))

    def test_profiled_send_button_rejects_upload_button(self) -> None:
        page = mock.Mock()
        upload = mock.Mock()
        upload.is_visible.return_value = True
        upload.is_enabled.return_value = True
        upload.evaluate.return_value = {
            "disabled": False,
            "ariaDisabled": "",
            "trackId": "home_upload_btn",
            "trackName": "home_upload_file",
            "text": "",
            "aria": "",
            "title": "",
            "viewBoxes": ["0 0 16 16"],
            "rect": {
                "left": 10,
                "right": 38,
                "top": 10,
                "bottom": 38,
                "width": 28,
                "height": 28,
                "viewportWidth": 1000,
                "viewportHeight": 800,
            },
        }
        locator = mock.Mock()
        locator.count.return_value = 1
        locator.nth.return_value = upload
        page.locator.return_value = locator

        result = mimo._profiled_send_button(page)

        self.assertIsNone(result)

    def test_profiled_send_button_rejects_stop_state_with_send_track_ids(self) -> None:
        page = mock.Mock()
        stop = mock.Mock()
        stop.is_visible.return_value = True
        stop.is_enabled.return_value = True
        stop.evaluate.return_value = {
            "disabled": False,
            "ariaDisabled": "",
            "trackId": "home_send_btn",
            "trackName": "home_send_message",
            "text": "终止回答",
            "aria": "",
            "title": "",
            "viewBoxes": ["0 0 24 24"],
            "rect": {
                "left": 10,
                "right": 38,
                "top": 10,
                "bottom": 38,
                "width": 28,
                "height": 28,
                "viewportWidth": 1000,
                "viewportHeight": 800,
            },
        }
        locator = mock.Mock()
        locator.count.return_value = 1
        locator.nth.return_value = stop
        page.locator.return_value = locator

        result = mimo._profiled_send_button(page)

        self.assertIsNone(result)

    def test_generation_complete_uses_finished_response_not_send_icon(self) -> None:
        page = mock.Mock()
        response = mock.Mock()
        copy_button = mock.Mock()

        with (
            mock.patch.object(mimo.controls, "locate_response", return_value=response),
            mock.patch.object(mimo, "_response_text", return_value="final reply"),
            mock.patch.object(mimo, "_response_is_typing", return_value=False),
            mock.patch.object(mimo, "_copy_button_after_response", return_value=copy_button),
        ):
            self.assertTrue(mimo._generation_complete(page))

    def test_generation_complete_rejects_typing_response(self) -> None:
        page = mock.Mock()
        response = mock.Mock()

        with (
            mock.patch.object(mimo.controls, "locate_response", return_value=response),
            mock.patch.object(mimo, "_response_text", return_value="partial reply"),
            mock.patch.object(mimo, "_response_is_typing", return_value=True),
            mock.patch.object(mimo, "_copy_button_after_response") as copy_button,
        ):
            self.assertFalse(mimo._generation_complete(page))

        copy_button.assert_not_called()

    def test_response_typing_state_is_explicitly_three_state(self) -> None:
        response = mock.Mock()
        for raw, expected in (
            ("true", True),
            (" TRUE ", True),
            ("false", False),
            (" False ", False),
            (None, None),
            ("", None),
            ("invalid", None),
        ):
            with self.subTest(raw=raw):
                response.evaluate.return_value = raw
                self.assertIs(mimo._response_typing_state(response), expected)

    def test_response_typing_state_dom_error_is_unknown(self) -> None:
        response = mock.Mock()
        response.evaluate.side_effect = RuntimeError("detached")

        self.assertIsNone(mimo._response_typing_state(response))
        self.assertFalse(mimo._response_is_typing(response))

    def test_response_typing_state_propagates_stop_and_deadline(self) -> None:
        response = mock.Mock()
        for error in (
            cancellation.TaskCancelled("stop"),
            cancellation.DeadlineExceeded("deadline"),
        ):
            with self.subTest(error=type(error).__name__):
                response.evaluate.side_effect = error
                with self.assertRaises(type(error)):
                    mimo._response_typing_state(response)

    def test_completion_observation_never_turns_unknown_into_false(self) -> None:
        response = mock.Mock()
        response.evaluate.return_value = None

        observation = mimo._completion_observation(
            response,
            current="stable reply",
            stable=True,
        )

        self.assertTrue(observation.response_nonempty)
        self.assertTrue(observation.response_stable)
        self.assertFalse(observation.typing_true)
        self.assertFalse(observation.typing_false)

    def test_generation_complete_accepts_finished_response_without_copy_when_not_generating(self) -> None:
        page = mock.Mock()
        response = mock.Mock()

        with (
            mock.patch.object(mimo.controls, "locate_response", return_value=response),
            mock.patch.object(mimo, "_response_text", return_value="final reply"),
            mock.patch.object(mimo, "_response_is_typing", return_value=False),
            mock.patch.object(mimo, "_copy_button_after_response", return_value=None),
            mock.patch.object(mimo, "_generation_active", return_value=False),
        ):
            self.assertTrue(mimo._generation_complete(page))

    def test_generation_complete_rejects_finished_text_while_generation_active(self) -> None:
        page = mock.Mock()
        response = mock.Mock()

        with (
            mock.patch.object(mimo.controls, "locate_response", return_value=response),
            mock.patch.object(mimo, "_response_text", return_value="partial reply"),
            mock.patch.object(mimo, "_response_is_typing", return_value=False),
            mock.patch.object(mimo, "_copy_button_after_response", return_value=None),
            mock.patch.object(mimo, "_generation_active", return_value=True),
        ):
            self.assertFalse(mimo._generation_complete(page))

    def test_uncertain_enter_submission_never_requests_a_second_action(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        with (
            mock.patch.object(mimo, "_message_box", return_value=textarea),
            mock.patch.object(mimo, "_send_button", return_value=None),
            mock.patch.object(mimo, "_wait_submission_started", return_value=False),
            mock.patch.object(mimo.controls, "request_teaching") as teaching,
        ):
            attempt = mimo._submit(page, baseline=0, submitted_text="hello")

        textarea.press.assert_called_once_with("Enter")
        teaching.assert_not_called()
        self.assertEqual(attempt.phase, "attempted")

    def test_send_button_uses_bounded_recovery_after_profile_fails(self) -> None:
        page = mock.Mock()
        recovered = mock.Mock()
        message_box = mock.Mock()
        with (
            mock.patch.object(mimo, "_profiled_send_button", return_value=None),
            mock.patch.object(mimo, "_message_box", return_value=message_box),
            mock.patch.object(mimo.controls, "locate_control", return_value=recovered) as locate,
            mock.patch.object(mimo.cancellation, "wait"),
        ):
            result = mimo._send_button(page, teach=True)

        self.assertIs(result, recovered)
        locate.assert_called_once_with(
            page,
            mimo.PROVIDER_ID,
            mimo.controls.CONTROL_SEND_BUTTON,
            (),
            require_enabled=True,
            teach=True,
            anchor=message_box,
        )

    def test_submit_refuses_enter_while_generation_active(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        with (
            mock.patch.object(mimo, "_message_box", return_value=textarea),
            mock.patch.object(mimo, "_send_button", return_value=None),
            mock.patch.object(mimo, "_generation_active", return_value=True),
        ):
            with self.assertRaisesRegex(TimeoutError, "still generating"):
                mimo._submit(page, baseline=0, submitted_text="hello")

        textarea.press.assert_not_called()

    def test_uncertain_submission_continues_until_delayed_answer(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        response = mock.Mock()
        response.evaluate.return_value = "false"
        attempt = SendAttempt()
        attempt.submit("click", lambda: None)
        with (
            mock.patch.object(mimo, "wait_ready"),
            mock.patch.object(mimo, "_message_box", return_value=textarea),
            mock.patch.object(mimo, "_submit", return_value=attempt),
            mock.patch.object(mimo, "_response_count", side_effect=[0, 1]),
            mock.patch.object(mimo.controls, "locate_response", return_value=response),
            mock.patch.object(mimo, "_response_text", return_value="delayed reply"),
            mock.patch.object(mimo, "_generation_complete", return_value=True),
            mock.patch.object(mimo, "_final_text", return_value="raw delayed reply"),
            mock.patch.object(mimo.controls, "control_has_text", return_value=True),
            mock.patch.object(mimo.controls, "confirm_control"),
            mock.patch.object(mimo.controls, "recover_flow") as recover_flow,
            mock.patch.object(mimo.cancellation, "wait"),
        ):
            reply = mimo.chat(
                page, "hello", response_timeout=1, stable_ticks=0, tick=0, min_wait=0
            )

        self.assertEqual(reply, "raw delayed reply")
        self.assertTrue(attempt.confirmed)
        recover_flow.assert_not_called()

    def test_stable_response_without_terminal_evidence_cannot_recover_flow(self) -> None:
        trace = mimo.provider_flow.FlowTrace()
        observation = mimo.provider_flow.FlowObservation(
            response_stable=True,
            response_nonempty=True,
        )
        trace.add(observation)
        trace.add(observation)
        helper = mock.Mock()

        with mock.patch.object(mimo.provider_flow, "_handler", helper):
            recipe = mimo.provider_flow.request_recovery(
                mimo.PROVIDER_ID,
                mimo.provider_flow.STAGE_COMPLETION,
                trace,
                object(),
            )

        self.assertIsNone(recipe)
        helper.assert_not_called()

    def test_typing_transition_can_recover_without_model_assistance(self) -> None:
        trace = mimo.provider_flow.FlowTrace()
        trace.add(mimo.provider_flow.FlowObservation(typing_true=True))
        terminal = mimo.provider_flow.FlowObservation(
            response_stable=True,
            response_nonempty=True,
            typing_false=True,
        )
        trace.add(terminal)
        trace.add(terminal)
        helper = mock.Mock()

        with mock.patch.object(mimo.provider_flow, "_handler", helper):
            recipe = mimo.provider_flow.request_recovery(
                mimo.PROVIDER_ID,
                mimo.provider_flow.STAGE_COMPLETION,
                trace,
                object(),
            )

        self.assertEqual(
            recipe,
            {
                mimo.provider_flow.STAGE_COMPLETION: (
                    mimo.provider_flow.PREDICATE_RESPONSE_STABLE,
                    mimo.provider_flow.PREDICATE_TYPING_FALSE,
                )
            },
        )
        helper.assert_not_called()

    def test_chat_wires_typing_transition_into_revival_transaction(self) -> None:
        page = mock.Mock(url="https://aistudio.xiaomimimo.com/#/c")
        textarea = mock.Mock()
        typing = mock.Mock()
        typing.evaluate.return_value = "true"
        terminal = mock.Mock()
        terminal.evaluate.return_value = "false"
        attempt = SendAttempt()
        attempt.submit("click", lambda: None)
        helper = mock.Mock()

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "provider-controls.json"
            mimo.controls.begin_task_context("mimo-chat-flow")
            try:
                with (
                    mock.patch.object(mimo.controls, "CONTROL_STORE", path),
                    mock.patch.object(mimo.provider_flow, "_handler", helper),
                    mock.patch.object(mimo, "wait_ready"),
                    mock.patch.object(mimo, "_message_box", return_value=textarea),
                    mock.patch.object(mimo, "_submit", return_value=attempt),
                    mock.patch.object(
                        mimo,
                        "_response_count",
                        side_effect=[0, 1, 1, 1, 1],
                    ),
                    mock.patch.object(
                        mimo.controls,
                        "locate_response",
                        side_effect=[typing, terminal, terminal, terminal],
                    ),
                    mock.patch.object(
                        mimo,
                        "_response_text",
                        side_effect=["", "final", "final", "final"],
                    ),
                    mock.patch.object(mimo, "_generation_complete", return_value=False),
                    mock.patch.object(mimo, "_final_text", return_value="final") as final_text,
                    mock.patch.object(mimo.controls, "control_has_text", return_value=True),
                    mock.patch.object(mimo.controls, "confirm_control"),
                    mock.patch.object(mimo.controls, "start_response_watch"),
                    mock.patch.object(mimo.controls, "stop_response_watch"),
                    mock.patch.object(mimo.cancellation, "wait"),
                ):
                    reply = mimo.chat(
                        page,
                        "hello",
                        response_timeout=1,
                        stable_ticks=1,
                        tick=0,
                        min_wait=0,
                    )
                meta = mimo.controls.load_controls(path)["mimo"]["_revival"]
            finally:
                mimo.controls.end_task_context()

        self.assertEqual(reply, "final")
        self.assertEqual(meta["status"], "provisional")
        final_text.assert_called_once_with(page, completion_verified=True)
        helper.assert_not_called()

    def test_typing_pause_or_missing_attribute_cannot_recover(self) -> None:
        helper = mock.Mock()
        with mock.patch.object(mimo.provider_flow, "_handler", helper):
            for terminal in (True, None):
                with self.subTest(terminal=terminal):
                    trace = mimo.provider_flow.FlowTrace()
                    trace.add(mimo.provider_flow.FlowObservation(typing_true=True))
                    observation = mimo.provider_flow.FlowObservation(
                        response_stable=True,
                        response_nonempty=True,
                        typing_true=terminal is True,
                        typing_false=terminal is False,
                    )
                    trace.add(observation)
                    trace.add(observation)
                    self.assertIsNone(
                        mimo.provider_flow.request_recovery(
                            mimo.PROVIDER_ID,
                            mimo.provider_flow.STAGE_COMPLETION,
                            trace,
                            object(),
                        )
                    )
        helper.assert_not_called()

    def test_initial_typing_false_without_start_evidence_cannot_recover(self) -> None:
        trace = mimo.provider_flow.FlowTrace()
        terminal = mimo.provider_flow.FlowObservation(
            response_stable=True,
            response_nonempty=True,
            typing_false=True,
        )
        trace.add(terminal)
        trace.add(terminal)

        self.assertIsNone(
            mimo.provider_flow.request_recovery(
                mimo.PROVIDER_ID,
                mimo.provider_flow.STAGE_COMPLETION,
                trace,
                object(),
            )
        )

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
            mock.patch.object(mimo, "_generation_complete", return_value=True),
            mock.patch.object(mimo, "_last_text", return_value='{"verdict":"approved"}'),
        ):
            self.assertEqual(mimo._final_text(object()), '{"verdict":"approved"}')

    def test_final_text_refuses_fallback_while_generating(self) -> None:
        with (
            mock.patch.object(mimo, "_copy_last_text", return_value=""),
            mock.patch.object(mimo, "_generation_complete", return_value=False),
            mock.patch.object(mimo, "_last_text") as last_text,
        ):
            with self.assertRaisesRegex(RuntimeError, "still generating"):
                mimo._final_text(object())

        last_text.assert_not_called()

    def test_final_text_rejects_missing_raw_and_visible_response(self) -> None:
        with (
            mock.patch.object(mimo, "_copy_last_text", return_value=""),
            mock.patch.object(mimo, "_generation_complete", return_value=True),
            mock.patch.object(mimo, "_last_text", return_value=""),
        ):
            with self.assertRaisesRegex(RuntimeError, "raw Xiaomi MiMo response"):
                mimo._final_text(object())


if __name__ == "__main__":
    unittest.main()
