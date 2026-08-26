from __future__ import annotations

import json
import threading
import unittest
from unittest import mock

from codey.runtime import cancellation
from codey.providers.submission import SendAttempt
from codey.providers.web_drivers import glm
from codey.research.protocols import JsonToolCodec


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

    def test_normalize_tool_json_reply_repairs_research_tool_call_before_codec(self) -> None:
        reply = '{“tool”:“web_search”,“args”:{“query”:“arXiv RAG evaluation”}}'

        plan = JsonToolCodec().parse(glm.normalize_tool_json_reply(reply))

        self.assertFalse(plan.protocol_error)
        self.assertEqual(plan.calls[0].name, "web_search")
        self.assertEqual(plan.calls[0].args["query"], "arXiv RAG evaluation")

    def test_normalize_tool_json_reply_repairs_research_done_answer_before_codec(self) -> None:
        reply = '{“tool”:“done”,“args”:{“answer”:“结论文本”}}'

        plan = JsonToolCodec().parse(glm.normalize_tool_json_reply(reply))

        self.assertFalse(plan.protocol_error)
        self.assertIsNotNone(plan.control)
        assert plan.control is not None
        self.assertEqual(plan.control.kind, "done")
        self.assertEqual(plan.control.body, "结论文本")

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

    def test_normalize_tool_json_reply_handles_extra_trailing_brace(self) -> None:
        reply = (
            '{“tool”:“done”,“args”:{“summary”:“扫描不完整，'
            'legacy/z_legacy_batch.py 被跳过。”}}}'
        )

        normalized = glm.normalize_tool_json_reply(reply)

        self.assertEqual(
            json.loads(normalized),
            {
                "tool": "done",
                "args": {"summary": "扫描不完整，legacy/z_legacy_batch.py 被跳过。"},
            },
        )

    def test_normalize_tool_json_reply_preserves_valid_summary_before_comma(self) -> None:
        reply = json.dumps({
            "tool": "done",
            "args": {"summary": "He said “stop”, then left"},
        }, ensure_ascii=False)

        self.assertEqual(glm.normalize_tool_json_reply(reply), reply)

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

    def test_normalize_tool_json_reply_preserves_python_edit_snippet_quotes(self) -> None:
        reply = json.dumps({
            "tool": "edit",
            "args": {
                "path": "routes.py",
                "old_string": "TITLE = '“Hello”'",
                "new_string": "return 'it’s ready'",
            },
        }, ensure_ascii=False)

        normalized = json.loads(glm.normalize_tool_json_reply(reply))

        self.assertEqual(normalized["args"]["old_string"], "TITLE = '“Hello”'")
        self.assertEqual(normalized["args"]["new_string"], "return 'it’s ready'")

    def test_normalize_tool_json_reply_preserves_valid_python_replacement_quotes(self) -> None:
        reply = json.dumps({
            "tool": "edit",
            "args": {
                "path": "app.py",
                "old_string": "TITLE = “stop”, then_continue()",
                "new_string": "TITLE = “go”, then_continue()",
            },
        }, ensure_ascii=False)

        self.assertEqual(glm.normalize_tool_json_reply(reply), reply)

    def test_normalize_tool_json_reply_repairs_mixed_smart_json_for_python_edit(self) -> None:
        reply = (
            '{"tool":"edit","args":{"path":"routes.py","old_string":'
            '"if not user.get(‘enabled’, False):",“new_string":'
            '"if not user.get(‘admin’, False):"}}'
        )

        normalized = json.loads(glm.normalize_tool_json_reply(reply))

        self.assertEqual(normalized["tool"], "edit")
        self.assertEqual(normalized["args"]["old_string"], "if not user.get(‘enabled’, False):")
        self.assertEqual(normalized["args"]["new_string"], "if not user.get(‘admin’, False):")

    def test_normalize_tool_json_reply_rejects_invalid_repair_candidate(self) -> None:
        reply = '{“tool”:“done”,“args”:'

        self.assertEqual(glm.normalize_tool_json_reply(reply), reply)

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

    def test_submitted_question_count_ignores_other_question_nodes(self) -> None:
        page = mock.Mock()
        page.locator.return_value.all_inner_texts.return_value = [
            "user\nolder tool result",
            "user\ncurrent prompt",
            "user\nnot current prompt",
            "user\nprotocol repair",
        ]

        self.assertEqual(glm._submitted_question_count(page, "current prompt"), 1)

    def test_rate_limit_visible_matches_glm_notice(self) -> None:
        page = mock.Mock()
        page.locator.return_value.inner_text.return_value = (
            "请求过于频繁，请稍后再试\n重新回答"
        )

        self.assertTrue(glm._rate_limit_visible(page))
        page.locator.assert_called_once_with("body")
        page.locator.return_value.inner_text.assert_called_once_with(timeout=1000)

    def test_click_rate_limit_retry_uses_latest_visible_button(self) -> None:
        page = mock.Mock()
        buttons = mock.Mock()
        hidden = mock.Mock()
        visible = mock.Mock()
        buttons.count.return_value = 2
        buttons.nth.side_effect = lambda index: [hidden, visible][index]
        hidden.is_visible.return_value = False
        visible.is_visible.return_value = True
        page.get_by_text.return_value = buttons

        with mock.patch.object(glm.cancellation, "wait") as wait:
            self.assertTrue(glm._click_rate_limit_retry(page))

        wait.assert_called_once_with(glm.RATE_LIMIT_COOLDOWN)
        page.get_by_text.assert_called_once_with("重新回答", exact=True)
        visible.click.assert_called_once_with()
        hidden.click.assert_not_called()

    def test_chat_duplicate_guard_counts_only_matching_submitted_prompt(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        attempt = SendAttempt()
        attempt.submit("click", lambda: None)
        with (
            mock.patch.object(glm, "wait_ready"),
            mock.patch.object(glm, "_message_box", return_value=textarea),
            mock.patch.object(glm, "_submit", return_value=attempt),
            mock.patch.object(glm, "_response_count", side_effect=[0, 1]),
            mock.patch.object(glm, "_question_count", return_value=3),
            mock.patch.object(glm, "_submitted_question_count", side_effect=[0, 1]),
            mock.patch.object(glm, "_last_text", return_value='{"tool":"done"}'),
            mock.patch.object(glm, "_generation_complete", return_value=True),
            mock.patch.object(glm, "_final_text", return_value='{"tool":"done"}'),
            mock.patch.object(glm.controls, "control_has_text", return_value=True),
            mock.patch.object(glm.controls, "confirm_control"),
            mock.patch.object(glm.cancellation, "wait"),
        ):
            reply = glm.chat(
                page,
                "current prompt",
                response_timeout=1,
                stable_ticks=0,
                tick=0,
                min_wait=0,
            )

        self.assertEqual(reply, '{"tool":"done"}')

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
            mock.patch.object(glm.controls, "recover_flow") as recover_flow,
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
        recover_flow.assert_not_called()
        sent = textarea.fill.call_args.args[0]
        self.assertTrue(sent.startswith("hello\n\n"))
        self.assertIn("ASCII U+0022", sent)

    def test_chat_stops_response_watch_once_after_driver_error(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        textarea.click.side_effect = RuntimeError("click failed")
        with (
            mock.patch.object(glm, "wait_ready"),
            mock.patch.object(glm, "_message_box", return_value=textarea),
            mock.patch.object(glm, "_response_count", return_value=0),
            mock.patch.object(glm, "_question_count", return_value=0),
            mock.patch.object(glm, "_submitted_question_count", return_value=0),
            mock.patch.object(glm.controls, "start_response_watch") as start_watch,
            mock.patch.object(glm.controls, "stop_response_watch") as stop_watch,
            mock.patch.object(glm.controls, "reject_control"),
        ):
            with self.assertRaisesRegex(RuntimeError, "click failed"):
                glm.chat(page, "hello", response_timeout=1, tick=0)

        start_watch.assert_called_once_with(page, glm.PROVIDER_ID)
        stop_watch.assert_called_once_with(page, glm.PROVIDER_ID)

    def test_stable_response_without_terminal_evidence_cannot_recover_flow(self) -> None:
        trace = glm.provider_flow.FlowTrace()
        observation = glm.provider_flow.FlowObservation(
            response_stable=True,
            response_nonempty=True,
        )
        trace.add(observation)
        trace.add(observation)
        helper = mock.Mock()

        with mock.patch.object(glm.provider_flow, "_handler", helper):
            recipe = glm.provider_flow.request_recovery(
                glm.PROVIDER_ID,
                glm.provider_flow.STAGE_COMPLETION,
                trace,
                object(),
            )

        self.assertIsNone(recipe)
        helper.assert_not_called()

    def test_chat_retries_repeated_rate_limits_and_waits_for_answer(self) -> None:
        page = mock.Mock()
        textarea = mock.Mock()
        attempt = SendAttempt()
        attempt.submit("click", lambda: None)
        with (
            mock.patch.object(glm, "wait_ready"),
            mock.patch.object(glm, "_message_box", return_value=textarea),
            mock.patch.object(glm, "_submit", return_value=attempt),
            mock.patch.object(glm, "_response_count", side_effect=[0, 0, 0, 1]),
            mock.patch.object(glm, "_question_count", return_value=0),
            mock.patch.object(glm, "_submitted_question_count", return_value=0),
            mock.patch.object(glm, "_last_text", return_value='{"tool":"done"}'),
            mock.patch.object(glm, "_rate_limit_visible", return_value=True),
            mock.patch.object(glm, "_click_rate_limit_retry", return_value=True) as retry,
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
        self.assertEqual(retry.call_count, 2)
        retry.assert_has_calls([mock.call(page), mock.call(page)])

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
            mock.patch.object(glm, "_question_count", return_value=0),
            mock.patch.object(glm, "_submitted_question_count", side_effect=[0, 2]),
            mock.patch.object(glm.controls, "control_has_text", return_value=True),
            mock.patch.object(glm.controls, "confirm_control"),
            mock.patch.object(glm.cancellation, "wait"),
        ):
            with self.assertRaisesRegex(RuntimeError, "more than once"):
                glm.chat(page, "hello", response_timeout=1, tick=0)

        self.assertEqual(attempt.method, "click")


if __name__ == "__main__":
    unittest.main()