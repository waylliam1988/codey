from __future__ import annotations

import json
import threading
import unittest
from unittest import mock

from codey import cancellation, glm
from codey.provider_submission import SendAttempt


class GlmDriverTests(unittest.TestCase):
    def test_cancelled_chat_exits_before_touching_page(self) -> None:
        event = threading.Event()
        event.set()
        page = mock.Mock()

        with cancellation.scope(event):
            with self.assertRaises(cancellation.TaskCancelled):
                glm.chat(page, "hello")

        page.locator.assert_not_called()
        page.goto.assert_not_called()

    def test_blank_message_is_rejected_before_touching_page(self) -> None:
        page = mock.Mock()

        with self.assertRaisesRegex(ValueError, "cannot be blank"):
            glm.chat(page, "   ")

        page.locator.assert_not_called()

    def test_prompt_hint_preserves_normal_prompt_and_scopes_json_requirement(self) -> None:
        prompt = glm.prepare_prompt("hello")

        self.assertTrue(prompt.startswith("hello\n\n"))
        self.assertIn("raw JSON object", prompt)
        self.assertIn("ASCII U+0022", prompt)
        self.assertIn("Preserve source-code punctuation exactly", prompt)
        self.assertIn("never use typographic smart quotes", prompt)
        self.assertIn("Do not wrap the JSON in markdown fences", prompt)
        self.assertNotIn("must answer with JSON", prompt)

    def test_normalize_tool_json_reply_is_glm_scoped(self) -> None:
        self.assertEqual(
            glm.normalize_tool_json_reply('{“tool”:“done”,“args”:{“summary”:“ok”}}'),
            '{"tool":"done","args":{"summary":"ok"}}',
        )
        self.assertEqual(
            glm.normalize_tool_json_reply('{“verdict”:“approved”,“findings”:[]}'),
            '{"verdict":"approved","findings":[]}',
        )
        prose = "普通回答：他说“tool”这个词。"
        self.assertEqual(glm.normalize_tool_json_reply(prose), prose)

    def test_normalize_tool_json_reply_preserves_smart_quotes_inside_summary(self) -> None:
        reply = '{“tool”:“done”,“args”:{“summary”:“构建“入场”掩码，并返回“安全”结果。”}}'

        normalized = glm.normalize_tool_json_reply(reply)

        self.assertEqual(
            json.loads(normalized),
            {
                "tool": "done",
                "args": {"summary": "构建“入场”掩码，并返回“安全”结果。"},
            },
        )

    def test_normalize_tool_json_reply_repairs_only_ast_valid_python_content(self) -> None:
        reply = '{“tool”:“edit”,“args”:{“path”:“app.py”,“content”:“def greeting():\\n return ‘hello’\\n”}}'

        normalized = glm.normalize_tool_json_reply(reply)

        self.assertEqual(
            json.loads(normalized)["args"]["content"],
            "def greeting():\n return 'hello'\n",
        )
        ascii_outer = json.dumps({
            "tool": "edit",
            "args": {"path": "app.py", "content": "def greeting():\n return ‘hello’\n"},
        }, ensure_ascii=False)
        self.assertEqual(
            json.loads(glm.normalize_tool_json_reply(ascii_outer))["args"]["content"],
            "def greeting():\n return 'hello'\n",
        )

    def test_normalize_tool_json_reply_does_not_guess_invalid_or_non_python_content(self) -> None:
        invalid = '{“tool”:“edit”,“args”:{“path”:“app.py”,“content”:“return ‘hello’”}}'
        javascript = '{“tool”:“edit”,“args”:{“path”:“app.js”,“content”:“const x = ‘hello’;”}}'

        self.assertIn("‘hello’", glm.normalize_tool_json_reply(invalid))
        self.assertIn("‘hello’", glm.normalize_tool_json_reply(javascript))

    def test_last_text_reads_only_profiled_final_answer(self) -> None:
        page = mock.Mock()
        response = mock.Mock()
        response.evaluate.return_value = True
        response.locator.return_value.all_inner_texts.return_value = []
        response.inner_text.return_value = '{"tool":"done"}'

        with mock.patch.object(glm.controls, "locate_response", return_value=response):
            result = glm._last_text(page)

        self.assertEqual(result, '{"tool":"done"}')
        response.evaluate.assert_called_once_with(
            glm._FINAL_ANSWER_NODE_JS,
            glm.THINKING_CONTENT,
        )
        response.inner_text.assert_called_once_with()

    def test_last_text_joins_split_markdown_segments_inside_one_answer(self) -> None:
        page = mock.Mock()
        response = mock.Mock()
        response.evaluate.return_value = True
        response.locator.return_value.all_inner_texts.return_value = [
            '{"tool":"edit","args":{"path":"README.md","content":"Run tests with:',
            'python -m unittest\\n```\\n"}}',
        ]

        with mock.patch.object(glm.controls, "locate_response", return_value=response):
            result = glm._last_text(page)

        self.assertEqual(
            result,
            '{"tool":"edit","args":{"path":"README.md","content":"Run tests with:\n'
            'python -m unittest\\n```\\n"}}',
        )
        response.inner_text.assert_not_called()

    def test_last_text_rejects_fallback_inside_thinking_area(self) -> None:
        page = mock.Mock()
        response = mock.Mock()
        response.evaluate.return_value = False

        with mock.patch.object(glm.controls, "locate_response", return_value=response):
            result = glm._last_text(page)

        self.assertEqual(result, "")
        response.inner_text.assert_not_called()

    def test_last_text_rejects_outer_fallback_containing_thinking_area(self) -> None:
        page = mock.Mock()
        response = mock.Mock()
        response.evaluate.return_value = False

        with mock.patch.object(glm.controls, "locate_response", return_value=response):
            result = glm._last_text(page)

        self.assertEqual(result, "")
        response.inner_text.assert_not_called()

    def test_generation_completion_uses_idle_composer_state(self) -> None:
        page = object()
        idle = object()
        with mock.patch.object(glm.controls, "visible_locator", return_value=idle) as visible:
            self.assertTrue(glm._generation_complete(page))

        visible.assert_called_once_with(page, glm.PROFILE.selector("idle_button"))

    def test_uncertain_submission_clicks_only_once(self) -> None:
        page = mock.Mock()
        button = mock.Mock()
        with (
            mock.patch.object(glm, "_send_button", return_value=button),
            mock.patch.object(glm, "_submission_started", return_value=False),
            mock.patch.object(glm.time, "time", side_effect=[0, 16]),
            mock.patch.object(glm.cancellation, "wait"),
        ):
            attempt = glm._submit(page, 0, 0, "hello")

        button.click.assert_called_once_with()
        self.assertEqual(attempt.phase, "attempted")

    def test_submission_started_accepts_new_question(self) -> None:
        with (
            mock.patch.object(glm, "_response_count", return_value=0),
            mock.patch.object(glm, "_question_count", return_value=2),
        ):
            started = glm._submission_started(object(), 0, 1, "hello")

        self.assertTrue(started)

    def test_submission_started_accepts_changed_text_without_count_increase(self) -> None:
        with (
            mock.patch.object(glm, "_response_count", return_value=1),
            mock.patch.object(glm, "_question_count", return_value=1),
            mock.patch.object(glm, "_last_text", return_value='{"verdict":"approved"}'),
        ):
            started = glm._submission_started(
                object(),
                1,
                1,
                "hello",
                baseline_text="previous response",
            )

        self.assertTrue(started)

    def test_wait_late_response_accepts_replaced_answer_without_count_increase(self) -> None:
        with (
            mock.patch.object(glm, "_response_count", return_value=1),
            mock.patch.object(glm, "_last_text", return_value='{"verdict":"approved"}'),
            mock.patch.object(glm, "_generation_complete", return_value=True),
            mock.patch.object(glm, "_final_text", return_value='{"verdict":"approved"}'),
        ):
            reply = glm._wait_late_response(
                object(),
                1,
                baseline_text="previous response",
                grace=0.01,
                tick=0,
            )

        self.assertEqual(reply, '{"verdict":"approved"}')

    def test_uncertain_submission_continues_until_delayed_answer(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        attempt = SendAttempt()
        attempt.submit("click", lambda: None)
        with (
            mock.patch.object(glm, "wait_ready"),
            mock.patch.object(glm, "_message_box", return_value=textarea),
            mock.patch.object(glm, "_submit", return_value=attempt),
            mock.patch.object(glm, "_response_count", side_effect=[0, 1]),
            mock.patch.object(glm, "_question_count", side_effect=[0, 1]),
            mock.patch.object(glm, "_last_text", return_value='{"tool":"done"}'),
            mock.patch.object(glm, "_generation_complete", return_value=True),
            mock.patch.object(glm, "_final_text", return_value='{"tool":"done"}'),
            mock.patch.object(glm.controls, "control_has_text", return_value=True),
            mock.patch.object(glm.controls, "confirm_control"),
            mock.patch.object(glm.cancellation, "wait"),
        ):
            reply = glm.chat(
                page,
                "hello",
                response_timeout=1,
                stable_ticks=0,
                tick=0,
                min_wait=0,
            )

        self.assertEqual(reply, '{"tool":"done"}')
        self.assertTrue(attempt.confirmed)
        sent = textarea.fill.call_args.args[0]
        self.assertTrue(sent.startswith("hello\n\n"))
        self.assertIn("ASCII U+0022", sent)

    def test_duplicate_question_is_reported_without_second_local_click(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        attempt = SendAttempt()
        attempt.submit("click", lambda: None)
        with (
            mock.patch.object(glm, "wait_ready"),
            mock.patch.object(glm, "_message_box", return_value=textarea),
            mock.patch.object(glm, "_submit", return_value=attempt),
            mock.patch.object(glm, "_response_count", return_value=0),
            mock.patch.object(glm, "_question_count", side_effect=[0, 2]),
            mock.patch.object(glm.controls, "control_has_text", return_value=True),
            mock.patch.object(glm.controls, "confirm_control"),
            mock.patch.object(glm.cancellation, "wait"),
        ):
            with self.assertRaisesRegex(RuntimeError, "more than once"):
                glm.chat(page, "hello", response_timeout=1, tick=0)

        self.assertEqual(attempt.method, "click")


if __name__ == "__main__":
    unittest.main()
