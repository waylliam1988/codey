from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from codey import cancellation
from codey import provider_flow as flow


class ProviderFlowTests(unittest.TestCase):
    def tearDown(self) -> None:
        flow.set_recovery_handler(None)
        flow.end_task_context()

    def test_recipe_accepts_only_bounded_known_predicates(self) -> None:
        self.assertEqual(
            flow.normalize_recipe(
                {"completion": ["response_stable", "stop_hidden"]}
            ),
            {"completion": ("response_stable", "stop_hidden")},
        )
        for recipe in (
            {"completion": ["response_stable"]},
            {"completion": ["stop_hidden"]},
            {"completion": ["css:.answer"]},
            {"completion": ["javascript"]},
            {"https://example.test": ["response_stable"]},
            {"response": ["response_stable"]},
            {"completion": ["response_stable"] * 4},
        ):
            with self.subTest(recipe=recipe):
                self.assertIsNone(flow.normalize_recipe(recipe))

    def test_completion_requires_nonempty_response(self) -> None:
        recipe = {"completion": ("response_stable", "stop_hidden")}
        trace = flow.FlowTrace()
        trace.add(flow.FlowObservation(stop_visible=True))
        terminal = flow.FlowObservation(
            response_stable=True,
            response_nonempty=True,
            stop_hidden=True,
        )
        trace.add(terminal)
        trace.add(terminal)

        self.assertFalse(
            flow.evaluate(
                recipe,
                flow.STAGE_COMPLETION,
                flow.FlowObservation(response_stable=True, stop_hidden=True),
                trace,
            )
        )
        self.assertTrue(
            flow.evaluate(
                recipe,
                flow.STAGE_COMPLETION,
                terminal,
                trace,
            )
        )

    def test_completion_requires_every_recipe_predicate(self) -> None:
        recipe = {"completion": ("response_stable", "stop_hidden")}
        trace = flow.FlowTrace()
        trace.add(flow.FlowObservation(stop_visible=True))
        observation = flow.FlowObservation(
            response_nonempty=True,
            stop_hidden=True,
        )
        trace.add(observation)
        trace.add(observation)

        self.assertFalse(
            flow.evaluate(recipe, flow.STAGE_COMPLETION, observation, trace)
        )

    def test_long_thinking_pause_without_terminal_transition_is_not_completion(self) -> None:
        trace = flow.FlowTrace()
        pause = flow.FlowObservation(
            response_stable=True,
            response_nonempty=True,
        )
        for _ in range(6):
            trace.add(pause)

        self.assertIsNone(
            flow.make_recovery_request(
                "qwen",
                flow.STAGE_COMPLETION,
                trace,
                object(),
            )
        )

    def test_streaming_pause_while_stop_remains_visible_is_not_completion(self) -> None:
        trace = flow.FlowTrace()
        trace.add(flow.FlowObservation(stop_visible=True))
        pause = flow.FlowObservation(
            response_stable=True,
            response_nonempty=True,
            stop_visible=True,
            typing_true=True,
        )
        trace.add(pause)
        trace.add(pause)

        self.assertIsNone(
            flow.make_recovery_request(
                "qwen",
                flow.STAGE_COMPLETION,
                trace,
                object(),
            )
        )

    def test_trace_is_boolean_only_and_bounded(self) -> None:
        trace = flow.FlowTrace(limit=2)
        trace.add(flow.FlowObservation(input_empty=True))
        trace.add(flow.FlowObservation(response_stable=True))
        trace.add(flow.FlowObservation(copy_visible=True))

        snapshot = trace.snapshot()
        self.assertEqual(len(snapshot), 2)
        self.assertTrue(all(isinstance(value, bool) for event in snapshot for value in event.values()))
        self.assertNotIn("input_empty", {key for event in snapshot for key, value in event.items() if value})

    def test_recovery_offers_only_repeated_locally_true_predicates(self) -> None:
        trace = flow.FlowTrace()
        trace.add(flow.FlowObservation(stop_visible=True))
        terminal = flow.FlowObservation(
            response_stable=True,
            response_nonempty=True,
            stop_hidden=True,
        )
        trace.add(terminal)
        trace.add(terminal)

        request = flow.make_recovery_request(
            "qwen", flow.STAGE_COMPLETION, trace, object()
        )

        self.assertIsNotNone(request)
        self.assertEqual(
            [candidate.predicates for candidate in request.candidates],
            [(flow.PREDICATE_RESPONSE_STABLE, flow.PREDICATE_STOP_HIDDEN)],
        )

    def test_recovery_runs_once_per_stage_and_suppresses_recursion(self) -> None:
        trace = flow.FlowTrace()
        trace.add(flow.FlowObservation(stop_visible=True, typing_true=True))
        observation = flow.FlowObservation(
            response_stable=True,
            response_nonempty=True,
            stop_hidden=True,
            typing_false=True,
            copy_visible=True,
        )
        trace.add(observation)
        trace.add(observation)
        nested_results: list[object] = []

        def handler(request: flow.FlowRecoveryRequest) -> str:
            nested_results.append(
                flow.request_recovery(
                    request.provider_id,
                    request.stage,
                    trace,
                    request.page,
                )
            )
            return request.candidates[0].candidate_id

        flow.begin_task_context("session-1")
        flow.set_recovery_handler(handler)
        first = flow.request_recovery("qwen", flow.STAGE_COMPLETION, trace, object())
        second = flow.request_recovery("qwen", flow.STAGE_COMPLETION, trace, object())

        self.assertEqual(
            first,
            {"completion": ("response_stable", "typing_false")},
        )
        self.assertIsNone(second)
        self.assertEqual(nested_results, [None])

    def test_single_safe_candidate_is_adopted_without_model_call(self) -> None:
        trace = flow.FlowTrace()
        trace.add(flow.FlowObservation(stop_visible=True))
        terminal = flow.FlowObservation(
            response_stable=True,
            response_nonempty=True,
            stop_hidden=True,
        )
        trace.add(terminal)
        trace.add(terminal)
        handler = mock.Mock()
        flow.set_recovery_handler(handler)

        recipe = flow.request_recovery(
            "qwen",
            flow.STAGE_COMPLETION,
            trace,
            object(),
        )

        self.assertEqual(
            recipe,
            {"completion": ("response_stable", "stop_hidden")},
        )
        handler.assert_not_called()

    def test_recipe_rejects_multiple_stages_until_verification_is_per_stage(self) -> None:
        self.assertIsNone(
            flow.normalize_recipe({
                "submission": ["input_empty"],
                "completion": ["response_stable", "stop_hidden"],
            })
        )

    def test_non_structural_failure_never_calls_recovery_handler(self) -> None:
        trace = flow.FlowTrace()
        trace.add(flow.FlowObservation(stop_visible=True))
        observation = flow.FlowObservation(
            response_stable=True,
            response_nonempty=True,
            stop_hidden=True,
        )
        trace.add(observation)
        trace.add(observation)
        handler = mock.Mock(return_value="f1")
        flow.set_recovery_handler(handler)

        for kind in (
            "transient",
            "rate_limited",
            "authentication_required",
            "challenge_required",
            "submission_uncertain",
        ):
            with self.subTest(kind=kind):
                self.assertIsNone(
                    flow.request_recovery(
                        "qwen",
                        flow.STAGE_COMPLETION,
                        trace,
                        object(),
                        failure_kind=kind,
                    )
                )
        handler.assert_not_called()

    def test_candidate_reply_must_be_exact_known_json(self) -> None:
        request = flow.FlowRecoveryRequest(
            provider_id="qwen",
            stage=flow.STAGE_COMPLETION,
            trace=(),
            candidates=(
                flow.FlowCandidate(
                    "f1",
                    "completion",
                    ("response_stable", "stop_hidden"),
                ),
            ),
            page=object(),
        )

        self.assertEqual(
            flow.choose_candidate(request, lambda _prompt: '{"candidate_id":"f1"}'),
            "f1",
        )
        for reply in (
            '{"candidate_id":"f2"}',
            '{"candidate_id":"f1","selector":".answer"}',
            "f1",
        ):
            with self.subTest(reply=reply):
                self.assertIsNone(flow.choose_candidate(request, lambda _prompt: reply))

    def test_prompt_contains_no_page_or_user_content(self) -> None:
        request = flow.FlowRecoveryRequest(
            provider_id="qwen",
            stage=flow.STAGE_COMPLETION,
            trace=({"response_stable": True},),
            candidates=(
                flow.FlowCandidate(
                    "f1",
                    "completion",
                    ("response_stable", "stop_hidden"),
                ),
            ),
            page=SimpleNamespace(url="https://secret.test/private"),
            session_id="private-session",
        )

        prompt = flow.render_prompt(request)

        self.assertIn("Select one bounded web-chat state rule.", prompt)
        self.assertNotIn("state predicate.", prompt)
        self.assertNotIn("secret.test", prompt)
        self.assertNotIn("private-session", prompt)
        payload = json.loads(prompt.split("\n", 1)[1])
        self.assertEqual(payload["target_provider"], "qwen")

    def test_stop_propagates_before_model_assistance(self) -> None:
        request = flow.FlowRecoveryRequest(
            provider_id="qwen",
            stage=flow.STAGE_COMPLETION,
            trace=(),
            candidates=(),
            page=object(),
        )
        with mock.patch.object(
            cancellation,
            "check",
            side_effect=cancellation.TaskCancelled("stop"),
        ):
            with self.assertRaises(cancellation.TaskCancelled):
                flow.choose_candidate(request, mock.Mock())

    def test_profile_hash_is_stable_and_profile_sensitive(self) -> None:
        profile = SimpleNamespace(
            provider_id="qwen",
            version=1,
            hosts=("chat.qwen.ai",),
            selectors_by_action={"response": ("article.answer",)},
        )
        first = flow.profile_hash(profile)
        second = flow.profile_hash(profile)
        changed = flow.profile_hash(
            SimpleNamespace(
                provider_id="qwen",
                version=2,
                hosts=profile.hosts,
                selectors_by_action=profile.selectors_by_action,
            )
        )

        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)


if __name__ == "__main__":
    unittest.main()
