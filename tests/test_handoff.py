from __future__ import annotations

import json
import unittest

from codey.agents.handoff import (
    ConversationContext,
    ConversationSnapshot,
    estimate_tokens,
    render_continuation_prompt,
    render_handoff,
    render_recovered_handoff,
)


class HandoffTests(unittest.TestCase):
    def test_token_estimate_treats_ascii_and_non_ascii_consistently(self) -> None:
        self.assertEqual(estimate_tokens("abcd"), 1)
        self.assertEqual(estimate_tokens("abcde"), 2)
        self.assertEqual(estimate_tokens("你好"), 2)
        self.assertEqual(estimate_tokens("ab你好"), 3)

    def test_render_handoff_keeps_only_bounded_facts(self) -> None:
        snapshot = ConversationSnapshot(
            mode="project",
            goal="Fix the calculator",
            project=r"E:\demo",
            provider_id="deepseek",
            changed_files=("app.py", "test_app.py", "app.py"),
            checks_passed=True,
            summary="Implemented and verified",
            latest_reply="x" * 3000,
        )

        payload = json.loads(render_handoff(snapshot))

        self.assertEqual(payload["goal"], "Fix the calculator")
        self.assertEqual(payload["changed_files"], ["app.py", "test_app.py"])
        self.assertEqual(payload["checks"], "passed")
        self.assertLess(len(payload["latest_model_reply"]), 2100)
        self.assertNotIn("handoff_summary", payload)

    def test_recovered_handoff_adds_bounded_visible_conversation(self) -> None:
        snapshot = ConversationSnapshot(
            mode="chat",
            goal="Discuss app design",
            provider_id="deepseek",
            latest_user="Earlier question",
            latest_reply="Earlier answer",
        )

        payload = json.loads(render_recovered_handoff(
            snapshot,
            "User: Discuss breathing app\nAssistant: Use a calm timer\n" + "x" * 20_000,
        ))

        self.assertEqual(payload["goal"], "Discuss app design")
        self.assertIn("User: Discuss breathing app", payload["recent_visible_conversation"])
        self.assertLessEqual(len(payload["recent_visible_conversation"]), 12_020)

    def test_soft_limit_prepares_one_overwritable_handoff(self) -> None:
        context = ConversationContext(hard_limit=100)
        context.begin_window("deepseek", "chat")
        first = ConversationSnapshot(
            mode="chat",
            goal="First goal",
            provider_id="deepseek",
        )

        context.record_exchange("p" * 200, "r" * 100, first)

        self.assertTrue(context.handoff_summary)
        self.assertIn("First goal", context.handoff_summary)

        second = ConversationSnapshot(
            mode="chat",
            goal="Second goal",
            provider_id="deepseek",
        )
        context.update_snapshot(second)

        self.assertIn("Second goal", context.handoff_summary)
        self.assertNotIn("First goal", context.handoff_summary)

    def test_model_summary_is_bounded_filtered_and_overwritten(self) -> None:
        context = ConversationContext(hard_limit=100)
        context.begin_window("deepseek", "chat")
        context.update_snapshot(ConversationSnapshot(mode="chat", goal="Build an app"))

        handoff = context.prepare_model_handoff(lambda _prompt: (
            'Here is the summary: {"goal":"Build an app","decisions":["Use SQLite"],'
            '"current_state":"Tests pass","ignored":"secret"}'
        ))
        payload = json.loads(handoff)

        self.assertEqual(payload["conversation_summary"]["decisions"], ["Use SQLite"])
        self.assertEqual(payload["conversation_summary"]["current_state"], "Tests pass")
        self.assertNotIn("ignored", payload["conversation_summary"])

        context.begin_window("deepseek", "chat")
        self.assertEqual(context.snapshot.conversation_summary, "")
        replaced = context.prepare_model_handoff(
            lambda _prompt: '{"current_state":"Deployment remains"}'
        )
        self.assertIn("Deployment remains", replaced)
        self.assertNotIn("Use SQLite", replaced)

    def test_model_summary_failure_falls_back_to_local_snapshot(self) -> None:
        context = ConversationContext()
        context.update_snapshot(ConversationSnapshot(mode="chat", goal="Keep working"))

        def fail(_prompt: str) -> str:
            raise RuntimeError("summary failed")

        handoff = context.prepare_model_handoff(fail)

        self.assertIn("Keep working", handoff)

    def test_hard_limit_model_switch_and_continue_plan_fresh_chat(self) -> None:
        context = ConversationContext(hard_limit=100)
        context.begin_window("deepseek", "chat")
        context.update_snapshot(ConversationSnapshot(mode="chat", goal="Keep this fact"))
        context.used_tokens = 70

        fresh, handoff = context.plan_request(
            provider_id="deepseek",
            mode="chat",
            next_prompt="a" * 20,
        )
        self.assertTrue(fresh)
        self.assertIn("Keep this fact", handoff)

        fresh, handoff = context.plan_request(
            provider_id="qwen",
            mode="chat",
        )
        self.assertTrue(fresh)
        self.assertIn("Keep this fact", handoff)

        fresh, handoff = context.plan_request(
            provider_id="deepseek",
            mode="chat",
            force_rollover=True,
        )
        self.assertTrue(fresh)
        self.assertIn("Keep this fact", handoff)

    def test_same_window_stays_in_same_provider_chat(self) -> None:
        context = ConversationContext(hard_limit=1000)
        context.begin_window("stepfun", "chat")

        fresh, handoff = context.plan_request(
            provider_id="stepfun",
            mode="chat",
            next_prompt="a" * 20,
        )

        self.assertFalse(fresh)
        self.assertEqual(handoff, "")

    def test_chat_to_project_transition_returns_handoff_without_clearing_snapshot(self) -> None:
        context = ConversationContext(hard_limit=1000)
        context.begin_window("deepseek", "chat")
        context.update_snapshot(ConversationSnapshot(
            mode="chat",
            goal="Build a small notes app",
            provider_id="deepseek",
            latest_user="Use SQLite or a flat file?",
            latest_reply="Use SQLite for simple local persistence.",
        ))

        fresh, handoff = context.plan_request(
            provider_id="deepseek",
            mode="project",
            project=r"E:\notes-app",
            next_prompt="Apply the plan here.",
        )

        self.assertTrue(fresh)
        self.assertIn("Build a small notes app", handoff)
        self.assertIn("Use SQLite", handoff)

    def test_chat_to_research_transition_returns_handoff_without_clearing_snapshot(self) -> None:
        context = ConversationContext(hard_limit=1000)
        context.begin_window("deepseek", "chat")
        context.update_snapshot(ConversationSnapshot(
            mode="chat",
            goal="Choose the storage layer",
            provider_id="deepseek",
            latest_user="SQLite or a flat file?",
            latest_reply="SQLite is better once querying matters.",
        ))

        fresh, handoff = context.plan_request(
            provider_id="deepseek",
            mode="research",
            next_prompt="Research that choice.",
        )

        self.assertTrue(fresh)
        self.assertIn("Choose the storage layer", handoff)
        self.assertIn("SQLite is better", handoff)
        self.assertTrue(context.initialized)
        self.assertEqual(context.mode, "chat")
        self.assertEqual(context.snapshot.goal, "Choose the storage layer")

    def test_continuation_prompt_hides_runtime_mechanics_from_model_reply(self) -> None:
        prompt = render_continuation_prompt('{"goal":"Keep going"}', "What next?")

        self.assertIn("Factual handoff", prompt)
        self.assertIn("What next?", prompt)
        self.assertIn("Do not mention", prompt)


if __name__ == "__main__":
    unittest.main()
