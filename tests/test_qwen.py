from __future__ import annotations

import unittest
import threading
import tempfile
from pathlib import Path
from unittest import mock

from codey import cancellation, provider_revival, qwen
from codey.local_store import read_json, write_json_atomic
from codey.provider_diagnostics import ControlMissing
from codey.provider_submission import SendAttempt, SubmissionUncertain


class QwenDriverTests(unittest.TestCase):
    def tearDown(self) -> None:
        qwen.controls.end_task_context()

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

    def test_new_chat_applies_one_budget_to_navigation_and_ready_wait(self) -> None:
        page = mock.Mock()
        with (
            mock.patch.object(qwen, "start_deadline", return_value=20.0) as start,
            mock.patch.object(qwen, "navigation_timeout_ms", return_value=2500) as nav,
            mock.patch.object(qwen, "remaining", return_value=1.25) as remaining,
            mock.patch.object(qwen, "wait_ready") as ready,
            mock.patch.object(qwen, "_wait_composer_ready") as composer_ready,
        ):
            qwen.new_chat(page, timeout=5.0)

        start.assert_called_once_with(5.0)
        nav.assert_called_once_with(20.0)
        page.goto.assert_called_once_with(
            qwen.QWEN_URL,
            wait_until="domcontentloaded",
            timeout=2500,
        )
        self.assertEqual(
            remaining.call_args_list,
            [
                mock.call(20.0, qwen.READY_TIMEOUT),
                mock.call(20.0, qwen.COMPOSER_READY_TIMEOUT),
            ],
        )
        ready.assert_called_once_with(page, timeout=1.25)
        composer_ready.assert_called_once_with(page, timeout=1.25)

    def test_late_grace_does_not_swallow_total_deadline(self) -> None:
        with (
            cancellation.deadline_scope(qwen.time.monotonic()),
            mock.patch.object(qwen, "_response_count", return_value=0),
        ):
            with self.assertRaises(cancellation.DeadlineExceeded):
                qwen._wait_late_response(mock.Mock(), 0, grace=60, tick=1)

    def test_learned_input_failure_reaches_revival_health(self) -> None:
        page = mock.Mock(url="https://chat.qwen.ai/")
        textarea = mock.Mock()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            provider_revival.complete_send(
                path,
                "qwen",
                "chat.qwen.ai",
                {"message_box": {"tag": "textarea", "placeholder": "Ask"}},
                {"message_box"},
                set(),
            )

            def learned_message_box(_page, *, teach=False):
                del teach
                qwen.controls._remember_source("qwen", "message_box", "learned")
                return textarea

            with (
                mock.patch.object(qwen.controls, "CONTROL_STORE", path),
                mock.patch.object(qwen, "wait_ready"),
                mock.patch.object(qwen, "_response_count", return_value=0),
                mock.patch.object(qwen, "_message_box", side_effect=learned_message_box),
                mock.patch.object(qwen, "_visible_locator", return_value=None),
                mock.patch.object(qwen, "_fill_message_until_stable", side_effect=ControlMissing("input lost")),
            ):
                with self.assertRaises(ControlMissing):
                    qwen.chat(page, "hello")

            meta = read_json(path)["qwen"]["_revival"]

        self.assertEqual(meta["failures"], 1)

    def test_missing_learned_send_button_rolls_back_through_submit_path(self) -> None:
        old = {
            "host": "chat.qwen.ai",
            "fingerprint": {"tag": "button", "aria_label": "Old send"},
            "verified": True,
            "failures": 0,
        }
        page = mock.Mock(url="https://chat.qwen.ai/")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            write_json_atomic(path, {"qwen": {"send_button": old}})
            provider_revival.complete_send(
                path,
                "qwen",
                "chat.qwen.ai",
                {"send_button": {"tag": "button", "aria_label": "New send"}},
                {"send_button"},
                set(),
            )
            with (
                mock.patch.object(qwen.controls, "CONTROL_STORE", path),
                mock.patch.object(qwen, "_send_button", return_value=None),
            ):
                for _ in range(2):
                    with self.assertRaises(ControlMissing):
                        qwen._submit(page, 0, "hello ")

            provider = read_json(path)["qwen"]

        self.assertEqual(provider["send_button"], old)
        self.assertNotIn("_revival", provider)

    def test_learned_response_read_failure_rolls_back_through_final_text(self) -> None:
        old = {
            "host": "chat.qwen.ai",
            "fingerprint": {"tag": "article", "classes": ["old-answer"]},
            "verified": True,
            "failures": 0,
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            write_json_atomic(path, {"qwen": {"response": old}})
            provider_revival.complete_send(
                path,
                "qwen",
                "chat.qwen.ai",
                {"response": {"tag": "article", "classes": ["new-answer"]}},
                {"response"},
                set(),
            )
            with (
                mock.patch.object(qwen.controls, "CONTROL_STORE", path),
                mock.patch.object(qwen, "_resolve_preference"),
                mock.patch.object(qwen, "_copy_last_text", return_value=""),
                mock.patch.object(qwen, "_last_text", return_value=""),
            ):
                for _ in range(2):
                    qwen.controls._remember_source("qwen", "response", "learned")
                    with self.assertRaisesRegex(RuntimeError, "Could not read"):
                        qwen._final_text(mock.Mock())

            provider = read_json(path)["qwen"]

        self.assertEqual(provider["response"], old)
        self.assertNotIn("_revival", provider)

    def test_uncertain_submission_without_click_error_does_not_penalize_button(self) -> None:
        page = mock.Mock(url="https://chat.qwen.ai/")
        textarea = mock.Mock()
        attempt = SendAttempt()
        attempt.submit("click", lambda: None)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            provider_revival.complete_send(
                path,
                "qwen",
                "chat.qwen.ai",
                {"send_button": {"tag": "button", "aria_label": "Send"}},
                {"send_button"},
                set(),
            )

            def uncertain_submit(*_args, **_kwargs):
                qwen.controls._remember_source("qwen", "send_button", "learned")
                return attempt

            with (
                mock.patch.object(qwen.controls, "CONTROL_STORE", path),
                mock.patch.object(qwen, "wait_ready"),
                mock.patch.object(qwen, "_response_count", return_value=0),
                mock.patch.object(qwen, "_message_box", return_value=textarea),
                mock.patch.object(qwen, "_wait_composer_ready", return_value=textarea),
                mock.patch.object(qwen, "_fill_message_until_stable", return_value="hello "),
                mock.patch.object(qwen, "_submit", side_effect=uncertain_submit),
                mock.patch.object(qwen, "_wait_late_response", return_value=""),
                mock.patch.object(qwen.controls, "recover_response", return_value=None),
            ):
                with self.assertRaises(SubmissionUncertain):
                    qwen.chat(page, "hello", response_timeout=0)

            provider = read_json(path)["qwen"]

        self.assertEqual(provider["_revival"]["failures"], 0)
        self.assertEqual(provider["send_button"]["failures"], 0)

    def test_uncertain_submission_with_click_error_penalizes_button(self) -> None:
        page = mock.Mock(url="https://chat.qwen.ai/")
        textarea = mock.Mock()
        attempt = SendAttempt()

        def click_failed():
            raise RuntimeError("detached")

        attempt.submit("click", click_failed)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            provider_revival.complete_send(
                path,
                "qwen",
                "chat.qwen.ai",
                {"send_button": {"tag": "button", "aria_label": "Send"}},
                {"send_button"},
                set(),
            )

            def failed_submit(*_args, **_kwargs):
                qwen.controls._remember_source("qwen", "send_button", "learned")
                return attempt

            with (
                mock.patch.object(qwen.controls, "CONTROL_STORE", path),
                mock.patch.object(qwen, "wait_ready"),
                mock.patch.object(qwen, "_response_count", return_value=0),
                mock.patch.object(qwen, "_message_box", return_value=textarea),
                mock.patch.object(qwen, "_wait_composer_ready", return_value=textarea),
                mock.patch.object(qwen, "_fill_message_until_stable", return_value="hello "),
                mock.patch.object(qwen, "_submit", side_effect=failed_submit),
                mock.patch.object(qwen, "_wait_late_response", return_value=""),
                mock.patch.object(qwen.controls, "recover_response", return_value=None),
            ):
                with self.assertRaises(SubmissionUncertain):
                    qwen.chat(page, "hello", response_timeout=0)

            provider = read_json(path)["qwen"]

        self.assertEqual(provider["_revival"]["failures"], 1)
        self.assertEqual(provider["send_button"]["failures"], 1)

    def test_wait_ready_accepts_stable_model_selector_without_bootstrap_resource(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        with (
            mock.patch.object(qwen, "_message_box", return_value=textarea),
            mock.patch.object(qwen, "_model_selector_text", return_value="Qwen3.7-Plus"),
            mock.patch.object(qwen.cancellation, "wait") as wait,
        ):
            qwen.wait_ready(page, timeout=1)

        self.assertEqual(wait.call_count, 1)

    def test_wait_ready_requires_model_selector_stability(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        with (
            mock.patch.object(qwen, "_message_box", return_value=textarea),
            mock.patch.object(
                qwen,
                "_model_selector_text",
                side_effect=["", "Qwen3.7-Plus", "Qwen3.7-Plus"],
            ) as model_text,
            mock.patch.object(qwen.cancellation, "wait") as wait,
        ):
            qwen.wait_ready(page, timeout=1)

        self.assertEqual(model_text.call_count, 3)
        self.assertEqual(wait.call_count, 2)

    def test_wait_ready_fallback_does_not_accept_one_selector_read(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        with (
            mock.patch.object(qwen, "_message_box", return_value=textarea),
            mock.patch.object(qwen, "_model_selector_text", return_value="Qwen3.7-Plus"),
        ):
            with self.assertRaisesRegex(TimeoutError, "model selector"):
                qwen.wait_ready(page, timeout=0)

    def test_wait_ready_rejects_changing_model_selector_until_timeout(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        now = 0.0

        def fake_time() -> float:
            return now

        def advance(seconds: float) -> None:
            nonlocal now
            now += seconds

        with (
            mock.patch.object(qwen.time, "time", side_effect=fake_time),
            mock.patch.object(qwen, "_message_box", return_value=textarea),
            mock.patch.object(
                qwen,
                "_model_selector_text",
                side_effect=[
                    "Qwen3.7-Plus",
                    "Qwen3.7-Flash",
                    "Qwen3.7-Plus",
                    "Qwen3.7-Flash",
                ],
            ),
            mock.patch.object(qwen.cancellation, "wait", side_effect=advance),
        ):
            with self.assertRaisesRegex(TimeoutError, "model selector"):
                qwen.wait_ready(page, timeout=1)

    def test_new_chat_tolerates_qwen_redirect_abort_before_ready(self) -> None:
        page = mock.Mock()
        page.goto.side_effect = qwen.PlaywrightError(
            "Page.goto: net::ERR_ABORTED at https://chat.qwen.ai/"
        )

        with (
            mock.patch.object(qwen, "wait_ready") as wait_ready,
            mock.patch.object(qwen, "_wait_composer_ready") as composer_ready,
        ):
            qwen.new_chat(page)

        wait_ready.assert_called_once_with(page)
        composer_ready.assert_called_once_with(page)

    def test_new_chat_keeps_unrelated_navigation_errors_visible(self) -> None:
        page = mock.Mock()
        page.goto.side_effect = qwen.PlaywrightError("Page.goto: net::ERR_FAILED")

        with self.assertRaises(qwen.PlaywrightError):
            qwen.new_chat(page)

    def test_model_selector_text_is_read_without_page_content(self) -> None:
        page = mock.Mock()
        page.evaluate.return_value = " Qwen3.7-Plus "

        self.assertEqual(qwen._model_selector_text(page), "Qwen3.7-Plus")

        page.evaluate.assert_called_once_with(qwen._MODEL_SELECTOR_TEXT_JS)

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

    def test_fill_message_until_stable_refills_after_qwen_clears_draft(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()

        with (
            mock.patch.object(qwen, "_fill_message", side_effect=["hello ", "hello "]) as fill,
            mock.patch.object(qwen, "_composer_accepts_submission", side_effect=[False, True]),
            mock.patch.object(qwen.cancellation, "wait") as wait,
        ):
            submitted_text = qwen._fill_message_until_stable(page, textarea, "hello")

        self.assertEqual(submitted_text, "hello ")
        self.assertEqual(fill.call_count, 2)
        wait.assert_called_once_with(qwen.COMPOSER_REFILL_DELAY)

    def test_fill_message_until_stable_rejects_repeated_page_clears(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()

        with (
            mock.patch.object(qwen, "_fill_message", return_value="hello "),
            mock.patch.object(qwen, "_composer_accepts_submission", return_value=False),
            mock.patch.object(qwen.cancellation, "wait"),
        ):
            with self.assertRaisesRegex(ControlMissing, "did not keep"):
                qwen._fill_message_until_stable(page, textarea, "hello")

    def test_composer_accepts_submission_requires_enabled_send_button(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        send = mock.Mock()

        with (
            mock.patch.object(qwen, "_composer_value", return_value="hello "),
            mock.patch.object(qwen, "_send_button", side_effect=[None, send, send]) as send_button,
            mock.patch.object(qwen.cancellation, "wait") as wait,
        ):
            accepted = qwen._composer_accepts_submission(
                page,
                textarea,
                "hello ",
            )

        self.assertTrue(accepted)
        self.assertEqual(send_button.call_count, 2)
        wait.assert_called_once_with(qwen.COMPOSER_READY_TICK)

    def test_wait_composer_ready_returns_interactive_composer_without_fixed_settle(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()

        with (
            mock.patch.object(qwen, "_message_box", return_value=textarea),
            mock.patch.object(qwen, "_composer_is_interactive", return_value=True),
            mock.patch.object(qwen, "_composer_value") as value,
            mock.patch.object(qwen, "_visible_locator", return_value=None),
            mock.patch.object(qwen.cancellation, "wait") as wait,
        ):
            found = qwen._wait_composer_ready(page, timeout=1)

        self.assertIs(found, textarea)
        value.assert_not_called()
        wait.assert_not_called()

    def test_wait_composer_ready_waits_until_generation_stops(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()

        with (
            mock.patch.object(qwen, "_message_box", return_value=textarea),
            mock.patch.object(qwen, "_composer_is_interactive", return_value=True),
            mock.patch.object(qwen, "_visible_locator", side_effect=[mock.Mock(), None]),
            mock.patch.object(qwen.cancellation, "wait") as wait,
        ):
            found = qwen._wait_composer_ready(page, timeout=1)

        self.assertIs(found, textarea)
        wait.assert_called_once_with(qwen.COMPOSER_READY_TICK)

    def test_composer_accepts_submission_rejects_late_hydration_clear(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()

        with (
            mock.patch.object(qwen, "_composer_value", side_effect=["hello ", ""]),
            mock.patch.object(qwen, "_send_button", return_value=mock.Mock()),
            mock.patch.object(qwen.cancellation, "wait"),
        ):
            accepted = qwen._composer_accepts_submission(
                page,
                textarea,
                "hello ",
            )

        self.assertFalse(accepted)

    def test_composer_has_submitted_text_ignores_qwen_trailing_space(self) -> None:
        textarea = mock.Mock()

        with mock.patch.object(qwen, "_composer_value", return_value="hello "):
            self.assertTrue(qwen._composer_has_submitted_text(textarea, "hello"))

    def test_submit_confirms_that_qwen_accepted_message(self) -> None:
        page = mock.Mock()
        send = mock.Mock()

        with mock.patch.object(qwen.controls, "visible_locator", return_value=send):
            qwen._submit(page, baseline=0)

        send.click.assert_called_once_with()

    def test_send_button_uses_strict_qwen_fallback_before_teaching(self) -> None:
        page = mock.Mock()
        button_list = mock.Mock()
        button = mock.Mock()
        button_list.count.return_value = 1
        button_list.nth.return_value = button
        button.is_visible.return_value = True
        button.is_enabled.return_value = True
        button.evaluate.return_value = {
            "disabled": False,
            "ariaDisabled": "",
            "className": "send-button  ",
        }
        page.locator.return_value = button_list

        with (
            mock.patch.object(qwen, "_message_box", return_value=mock.Mock()),
            mock.patch.object(qwen.controls, "locate_control", return_value=None) as locate,
        ):
            found = qwen._send_button(page, teach=True)

        self.assertIs(found, button)
        locate.assert_called_once()
        page.locator.assert_called_once_with("button.send-button")

    def test_strict_qwen_send_fallback_rejects_disabled_class(self) -> None:
        page = mock.Mock()
        button_list = mock.Mock()
        button = mock.Mock()
        button_list.count.return_value = 1
        button_list.nth.return_value = button
        button.is_visible.return_value = True
        button.is_enabled.return_value = True
        button.evaluate.return_value = {
            "disabled": False,
            "ariaDisabled": "",
            "className": "send-button disabled ",
        }
        page.locator.return_value = button_list

        self.assertIsNone(qwen._qwen_enabled_send_button(page))

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

    def test_submit_clicks_after_qwen_draft_was_prepared(self) -> None:
        page = mock.Mock()
        send = mock.Mock()
        events = []
        send.click.side_effect = lambda: events.append("click")
        textarea = mock.Mock()

        with (
            mock.patch.object(qwen, "_send_button", return_value=send),
            mock.patch.object(qwen, "_message_box", return_value=textarea),
            mock.patch.object(qwen, "_composer_value", return_value="hello "),
            mock.patch.object(qwen, "_submission_started", return_value=True),
            mock.patch.object(
                qwen.cancellation,
                "wait",
                side_effect=lambda seconds: events.append(("wait", seconds)),
            ),
        ):
            qwen._submit(page, baseline=0, submitted_text="hello ")

        self.assertEqual(events, ["click"])

    def test_submit_rechecks_state_before_single_click(self) -> None:
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
        textarea = mock.Mock()

        with (
            mock.patch.object(qwen, "_send_button", return_value=send),
            mock.patch.object(qwen, "_message_box", return_value=textarea),
            mock.patch.object(qwen, "_composer_value", return_value="hello"),
            mock.patch.object(qwen, "_submission_started", return_value=False),
            mock.patch.object(qwen.time, "time", side_effect=[0, 16]),
            mock.patch.object(qwen.cancellation, "wait"),
        ):
            attempt = qwen._submit(page, baseline=0, submitted_text="hello")

        send.click.assert_called_once_with()
        self.assertEqual(attempt.phase, "attempted")

    def test_submit_rejects_lost_message_before_click_without_sending(self) -> None:
        page = mock.Mock()
        send = mock.Mock()
        textarea = mock.Mock()

        with (
            mock.patch.object(qwen, "_send_button", return_value=send),
            mock.patch.object(qwen, "_message_box", return_value=textarea),
            mock.patch.object(qwen, "_composer_value", return_value=""),
        ):
            with self.assertRaisesRegex(ControlMissing, "lost the message"):
                qwen._submit(page, baseline=0, submitted_text="hello ")

        send.click.assert_not_called()

    def test_uncertain_submission_reports_sanitized_click_error_type(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        attempt = SendAttempt()
        attempt.submit("click", lambda: (_ for _ in ()).throw(ValueError("private")))
        with (
            mock.patch.object(qwen, "wait_ready"),
            mock.patch.object(qwen, "_message_box", return_value=textarea),
            mock.patch.object(qwen, "_wait_composer_ready", return_value=textarea),
            mock.patch.object(qwen, "_fill_message_until_stable", return_value="hello "),
            mock.patch.object(qwen, "_submit", return_value=attempt),
            mock.patch.object(qwen, "_response_count", return_value=0),
            mock.patch.object(qwen, "_last_text", return_value=""),
            mock.patch.object(qwen, "_empty_response_visible", return_value=False),
            mock.patch.object(qwen, "_wait_late_response", return_value=""),
            mock.patch.object(qwen.controls, "recover_response", return_value=None),
            mock.patch.object(qwen.controls, "reject_control"),
            mock.patch.object(qwen.cancellation, "wait"),
        ):
            with self.assertRaisesRegex(
                SubmissionUncertain,
                "click failed with ValueError",
            ) as caught:
                qwen.chat(page, "hello", response_timeout=0)

        self.assertNotIn("private", str(caught.exception))

    def test_chat_stops_response_watch_once_after_driver_error(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        with (
            mock.patch.object(qwen, "wait_ready"),
            mock.patch.object(qwen, "_message_box", return_value=textarea),
            mock.patch.object(qwen, "_wait_composer_ready", return_value=textarea),
            mock.patch.object(qwen, "_fill_message_until_stable", side_effect=RuntimeError("fill failed")),
            mock.patch.object(qwen, "_response_count", return_value=0),
            mock.patch.object(qwen.controls, "start_response_watch") as start_watch,
            mock.patch.object(qwen.controls, "stop_response_watch") as stop_watch,
            mock.patch.object(qwen.controls, "reject_control"),
        ):
            with self.assertRaisesRegex(RuntimeError, "fill failed"):
                qwen.chat(page, "hello", response_timeout=1, tick=0)

        start_watch.assert_called_once_with(page, qwen.PROVIDER_ID)
        stop_watch.assert_called_once_with(page, qwen.PROVIDER_ID)

    def test_uncertain_submission_continues_until_delayed_answer(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        attempt = SendAttempt()
        attempt.submit("click", lambda: None)
        with (
            mock.patch.object(qwen, "wait_ready"),
            mock.patch.object(qwen, "_message_box", return_value=textarea),
            mock.patch.object(qwen, "_wait_composer_ready", return_value=textarea),
            mock.patch.object(qwen, "_fill_message_until_stable", return_value="hello "),
            mock.patch.object(qwen, "_submit", return_value=attempt),
            mock.patch.object(qwen, "_response_count", side_effect=[0, 1]),
            mock.patch.object(qwen, "_last_text", return_value="delayed reply"),
            mock.patch.object(qwen, "_empty_response_visible", return_value=False),
            mock.patch.object(qwen, "_generation_complete", return_value=True),
            mock.patch.object(qwen, "_final_text", return_value="raw delayed reply"),
            mock.patch.object(qwen.controls, "confirm_control"),
            mock.patch.object(qwen.controls, "recover_flow") as recover_flow,
            mock.patch.object(qwen.cancellation, "wait"),
        ):
            reply = qwen.chat(
                page, "hello", response_timeout=1, stable_ticks=0, tick=0, min_wait=0
            )

        self.assertEqual(reply, "raw delayed reply")
        self.assertTrue(attempt.confirmed)
        recover_flow.assert_not_called()

    def test_confirmed_stable_response_can_use_bounded_flow_fallback(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        attempt = SendAttempt()
        attempt.submit("click", lambda: None)
        with (
            mock.patch.object(qwen, "wait_ready"),
            mock.patch.object(qwen, "_message_box", return_value=textarea),
            mock.patch.object(qwen, "_wait_composer_ready", return_value=textarea),
            mock.patch.object(qwen, "_fill_message_until_stable", return_value="hello "),
            mock.patch.object(qwen, "_submit", return_value=attempt),
            mock.patch.object(qwen, "_response_count", side_effect=[0, 1, 1, 1]),
            mock.patch.object(qwen, "_last_text", return_value="stable reply"),
            mock.patch.object(qwen, "_empty_response_visible", return_value=False),
            mock.patch.object(qwen, "_generation_complete", return_value=False),
            mock.patch.object(qwen, "_final_text", return_value="raw stable reply"),
            mock.patch.object(
                qwen,
                "_visible_locator",
                side_effect=[mock.Mock(), mock.Mock(), None],
            ),
            mock.patch.object(qwen.controls, "confirm_control"),
            mock.patch.object(qwen.provider_flow, "_handler") as helper,
            mock.patch.object(qwen.cancellation, "wait"),
        ):
            reply = qwen.chat(
                page, "hello", response_timeout=1, stable_ticks=0, tick=0, min_wait=0
            )

        self.assertEqual(reply, "raw stable reply")
        self.assertTrue(attempt.confirmed)
        helper.assert_not_called()

    def test_chat_returns_stable_json_tool_reply_without_waiting_for_page_completion(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        attempt = SendAttempt()
        attempt.submit("click", lambda: None)
        json_reply = '{"tool":"done","args":{"answer":"ok"}}'
        with (
            mock.patch.object(qwen, "wait_ready"),
            mock.patch.object(qwen, "_message_box", return_value=textarea),
            mock.patch.object(qwen, "_wait_composer_ready", return_value=textarea),
            mock.patch.object(qwen, "_fill_message_until_stable", return_value="hello "),
            mock.patch.object(qwen, "_submit", return_value=attempt),
            mock.patch.object(qwen, "_response_count", side_effect=[0, *([1] * 20)]),
            mock.patch.object(qwen, "_last_text", return_value=json_reply),
            mock.patch.object(qwen, "_empty_response_visible", return_value=False),
            mock.patch.object(qwen, "_generation_complete", return_value=False),
            mock.patch.object(qwen, "_final_text", return_value=json_reply),
            mock.patch.object(qwen.controls, "confirm_control"),
            mock.patch.object(qwen.controls, "flow_stage_ready", return_value=False) as flow_ready,
            mock.patch.object(qwen.cancellation, "wait"),
        ):
            reply = qwen.chat(
                page,
                "hello",
                response_timeout=1,
                stable_ticks=99,
                tick=0,
                min_wait=0,
            )

        self.assertEqual(reply, json_reply)
        self.assertLess(flow_ready.call_count, 3)

    def test_chat_waits_for_ready_composer_before_filling(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        attempt = SendAttempt()
        attempt.submit("click", lambda: None)
        with (
            mock.patch.object(qwen, "wait_ready") as ready,
            mock.patch.object(qwen, "_wait_composer_ready", return_value=textarea) as composer_ready,
            mock.patch.object(qwen, "_message_box", return_value=textarea),
            mock.patch.object(qwen, "_response_count", return_value=0),
            mock.patch.object(qwen, "_last_text", return_value=""),
            mock.patch.object(qwen, "_fill_message_until_stable", return_value="hello ") as fill,
            mock.patch.object(qwen, "_submit", return_value=attempt),
            mock.patch.object(qwen, "_empty_response_visible", return_value=False),
            mock.patch.object(qwen, "_wait_late_response", return_value="late reply"),
        ):
            reply = qwen.chat(page, "hello", response_timeout=0)

        self.assertEqual(reply, "late reply")
        ready.assert_called_once_with(page)
        composer_ready.assert_called_once_with(page, teach=True)
        fill.assert_called_once_with(page, textarea, "hello")

    def test_chat_does_not_repeat_whole_send_after_slow_submit_confirmation(self) -> None:
        page = mock.Mock()
        with mock.patch.object(qwen, "_chat", side_effect=SubmissionUncertain("uncertain")) as chat_once:
            with self.assertRaisesRegex(SubmissionUncertain, "uncertain"):
                qwen.chat(page, "hello", response_timeout=1)

        chat_once.assert_called_once_with(page, "hello", 1, 2, 0.8, 1.5)

    def test_chat_does_not_retry_unrelated_timeout(self) -> None:
        page = mock.Mock()
        with (
            mock.patch.object(qwen, "_chat", side_effect=TimeoutError("input missing")) as chat_once,
        ):
            with self.assertRaisesRegex(TimeoutError, "input missing"):
                qwen.chat(page, "hello", response_timeout=1)

        chat_once.assert_called_once()

    def test_submission_started_requires_generation_or_new_response(self) -> None:
        with (
            mock.patch.object(qwen.controls, "visible_locator", return_value=None),
            mock.patch.object(qwen, "_response_count", return_value=0),
        ):
            started = qwen._submission_started(mock.Mock(), baseline=0)

        self.assertFalse(started)

    def test_submission_started_does_not_accept_cleared_input_alone(self) -> None:
        page = mock.Mock()
        with (
            mock.patch.object(qwen, "_visible_locator", return_value=None),
            mock.patch.object(qwen, "_response_count", return_value=0),
            mock.patch.object(qwen.controls, "control_has_text") as control_has_text,
            mock.patch.object(qwen.controls, "flow_matches", return_value=False),
        ):
            started = qwen._submission_started(page, baseline=0, submitted_text="hello ")

        self.assertFalse(started)
        control_has_text.assert_not_called()

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
            mock.patch.object(qwen, "_wait_composer_ready", return_value=textarea),
            mock.patch.object(qwen, "_submit"),
            mock.patch.object(qwen, "_response_count", side_effect=[0, 1]),
            mock.patch.object(qwen, "_last_text", return_value="recovered"),
            mock.patch.object(qwen, "_empty_response_visible", side_effect=[True, False]),
            mock.patch.object(qwen, "_generation_complete", return_value=True),
            mock.patch.object(qwen, "_regenerate_empty_response", return_value=True) as regenerate,
            mock.patch.object(qwen, "_final_text", return_value="raw recovered"),
            mock.patch.object(qwen, "_fill_message_until_stable", return_value="hello "),
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
            mock.patch.object(qwen, "_last_text", return_value="raw reply"),
        ):
            result = qwen._final_text(object())

        self.assertEqual(result, "raw reply")
        self.assertEqual(calls, ["preference", "copy"])

    def test_final_text_prefers_dom_json_when_copy_is_stale_prompt(self) -> None:
        json_reply = '{"tool":"done","args":{"answer":"ok"}}'
        with (
            mock.patch.object(qwen, "_resolve_preference", return_value=False),
            mock.patch.object(qwen, "_copy_last_text", return_value="previous prompt"),
            mock.patch.object(qwen, "_last_text", return_value=json_reply),
            mock.patch.object(qwen.controls, "confirm_control") as confirm_control,
        ):
            raw = qwen._final_text(object())

        self.assertEqual(raw, json_reply)
        confirm_control.assert_called_once_with(qwen.PROVIDER_ID, qwen.controls.CONTROL_RESPONSE)

    def test_final_text_repairs_missing_trailing_json_tool_brace(self) -> None:
        incomplete = '{"tool":"done","args":{"answer":"ok"}'
        complete = '{"tool":"done","args":{"answer":"ok"}}'
        with (
            mock.patch.object(qwen, "_resolve_preference", return_value=False),
            mock.patch.object(qwen, "_copy_last_text", return_value=incomplete),
            mock.patch.object(qwen, "_last_text", return_value=incomplete),
            mock.patch.object(qwen.controls, "confirm_control"),
        ):
            raw = qwen._final_text(object())

        self.assertEqual(raw, complete)

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
