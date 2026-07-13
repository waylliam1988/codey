from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey import provider_controls as controls


class ProviderControlsTests(unittest.TestCase):
    def tearDown(self) -> None:
        controls.set_teach_handler(None)
        controls.set_doctor_handler(None)
        controls.end_task_context()

    def test_task_context_cleanup_removes_all_task_local_state(self) -> None:
        page = mock.Mock()
        controls.begin_task_context("session-1")
        controls._doctor_attempts().add(("session-1", "qwen", "send_button"))
        controls._remember_source("qwen", controls.CONTROL_SEND_BUTTON, "pending")
        controls._remember_pending(
            "qwen",
            controls.CONTROL_SEND_BUTTON,
            page,
            {"tag": "button"},
        )
        controls._response_locator_map()["qwen"] = mock.Mock()
        controls._context.response_watches = {"qwen": "watch"}

        controls.end_task_context()

        for name in controls._TASK_CONTEXT_FIELDS:
            self.assertFalse(hasattr(controls._context, name), name)

    def test_save_control_overwrites_latest_teaching(self) -> None:
        page = mock.Mock()
        page.url = "https://chat.qwen.ai/"
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            controls.save_control(
                "qwen",
                controls.CONTROL_SEND_BUTTON,
                page,
                {"tag": "button", "text": "old"},
                path=path,
            )
            controls.save_control(
                "qwen",
                controls.CONTROL_SEND_BUTTON,
                page,
                {"tag": "button", "text": "new"},
                path=path,
            )

            data = controls.load_controls(path)

        self.assertEqual(data["qwen"][controls.CONTROL_SEND_BUTTON]["fingerprint"]["text"], "new")
        self.assertEqual(len(data["qwen"]), 1)

    def test_selector_candidates_prefer_stable_data_attributes(self) -> None:
        fingerprint = controls.fingerprint_from_click({
            "tag": "button",
            "role": "button",
            "text": "Send",
            "classes": ["abc123", "send-button"],
            "data": {"data-testid": "composer-send"},
        })

        selectors = controls.selector_candidates(fingerprint, controls.CONTROL_SEND_BUTTON)

        self.assertIn('button[data-testid="composer-send"]', selectors)
        self.assertIn('button[class~="send-button"]', selectors)

    def test_selector_candidates_never_fall_back_to_generic_role_or_editable(self) -> None:
        button = controls.fingerprint_from_click({"tag": "button", "role": "button"})
        editable = controls.fingerprint_from_click({"tag": "div", "contentEditable": True})

        self.assertEqual(controls.selector_candidates(button, controls.CONTROL_SEND_BUTTON), [])
        self.assertEqual(controls.selector_candidates(editable, controls.CONTROL_MESSAGE_BOX), [])

    def test_saved_control_rejects_ambiguous_visible_matches(self) -> None:
        page = mock.Mock()
        page.url = "https://chat.qwen.ai/"
        locator = mock.Mock()
        locator.count.return_value = 2
        locator.nth.return_value.is_visible.return_value = True
        page.locator.return_value = locator
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            controls.save_control(
                "qwen",
                controls.CONTROL_SEND_BUTTON,
                page,
                controls.fingerprint_from_click({"tag": "button", "ariaLabel": "Send"}),
                path=path,
            )
            with mock.patch.object(controls, "CONTROL_STORE", path):
                result = controls.saved_control(page, "qwen", controls.CONTROL_SEND_BUTTON)

        self.assertIsNone(result)

    def test_locate_control_reuses_pending_control_on_same_page(self) -> None:
        page = mock.Mock()
        page.url = "https://chat.qwen.ai/"
        missing = mock.Mock()
        missing.count.return_value = 0
        locator = mock.Mock()
        candidate = mock.Mock()
        candidate.is_visible.return_value = True
        candidate.is_enabled.return_value = True
        locator.count.return_value = 1
        locator.nth.return_value = candidate
        page.locator.side_effect = lambda selector: (
            missing if "data-codey-fault" in selector else locator
        )
        controls._remember_pending(
            "qwen",
            controls.CONTROL_SEND_BUTTON,
            page,
            controls.fingerprint_from_click({"tag": "button", "ariaLabel": "Send"}),
        )
        controls._remember_source(
            "qwen",
            controls.CONTROL_SEND_BUTTON,
            "pending",
        )

        with (
            mock.patch.object(controls, "saved_control") as saved,
            mock.patch.object(controls, "discover_control") as discover,
        ):
            result = controls.locate_control(
                page,
                "qwen",
                controls.CONTROL_SEND_BUTTON,
                ('[data-codey-fault="send"]',),
                require_enabled=True,
            )

        self.assertIs(result, candidate)
        saved.assert_not_called()
        discover.assert_not_called()
        self.assertEqual(
            controls._source_for("qwen", controls.CONTROL_SEND_BUTTON),
            "pending",
        )

    def test_pending_control_is_not_reused_on_another_page(self) -> None:
        original_page = mock.Mock()
        other_page = mock.Mock()
        controls._remember_pending(
            "qwen",
            controls.CONTROL_SEND_BUTTON,
            original_page,
            controls.fingerprint_from_click({"tag": "button", "ariaLabel": "Send"}),
        )

        result = controls.pending_control(
            other_page,
            "qwen",
            controls.CONTROL_SEND_BUTTON,
            require_enabled=True,
        )

        self.assertIsNone(result)
        other_page.locator.assert_not_called()

    def test_pending_control_reuses_verified_locator_without_stable_selector(self) -> None:
        page = mock.Mock()
        candidate = mock.Mock()
        candidate.is_visible.return_value = True
        candidate.is_enabled.return_value = True
        controls._remember_pending(
            "qwen",
            controls.CONTROL_SEND_BUTTON,
            page,
            controls.fingerprint_from_click({"tag": "button", "role": "button"}),
            candidate,
        )

        result = controls.pending_control(
            page,
            "qwen",
            controls.CONTROL_SEND_BUTTON,
            require_enabled=True,
        )

        self.assertIs(result, candidate)
        page.locator.assert_not_called()

    def test_candidate_scoring_requires_semantics_or_proximity_for_send(self) -> None:
        generic = {
            "fingerprint": {"tag": "button", "role": "button"},
            "bottom_ratio": 0.9,
            "anchor_distance": 600,
        }
        send = {
            "fingerprint": {"tag": "button", "role": "button", "ariaLabel": "Send message"},
            "bottom_ratio": 0.9,
            "anchor_distance": 40,
        }
        anchor = {"x": 0, "y": 0, "width": 100, "height": 40}

        self.assertLess(
            controls.score_control_candidate(generic, controls.CONTROL_SEND_BUTTON, anchor),
            62,
        )
        self.assertGreaterEqual(
            controls.score_control_candidate(send, controls.CONTROL_SEND_BUTTON, anchor),
            62,
        )

    def test_icon_send_needs_primary_filled_evidence_to_beat_nearby_action(self) -> None:
        anchor = {"x": 0, "y": 0, "width": 100, "height": 40}
        nearby = {
            "fingerprint": {"tag": "div", "role": "button", "classes": ["icon-label-primary", "capsule"]},
            "bottom_ratio": 0.9,
            "anchor_distance": 250,
        }
        send = {
            "fingerprint": {"tag": "div", "role": "button", "classes": ["button-primary", "filled", "circle"]},
            "bottom_ratio": 0.9,
            "anchor_distance": 300,
        }

        self.assertGreaterEqual(
            controls.score_control_candidate(send, controls.CONTROL_SEND_BUTTON, anchor)
            - controls.score_control_candidate(nearby, controls.CONTROL_SEND_BUTTON, anchor),
            12,
        )

    def test_enter_class_div_is_a_bounded_send_candidate(self) -> None:
        anchor = {"x": 0, "y": 0, "width": 400, "height": 80}
        candidate = {
            "fingerprint": {
                "tag": "div",
                "classes": ["enter", "is-main-chat"],
            },
            "bottom_ratio": 0.9,
            "anchor_distance": 40,
            "enabled": True,
        }
        fingerprint = controls.fingerprint_from_click(candidate["fingerprint"])

        self.assertGreaterEqual(
            controls.score_control_candidate(
                candidate,
                controls.CONTROL_SEND_BUTTON,
                anchor,
            ),
            62,
        )
        self.assertTrue(
            controls.control_fingerprint_is_valid(
                fingerprint,
                controls.CONTROL_SEND_BUTTON,
            )
        )

    def test_generic_div_is_not_a_send_candidate(self) -> None:
        candidate = {
            "fingerprint": {"tag": "div", "classes": ["model-select-item"]},
            "bottom_ratio": 0.9,
            "anchor_distance": 40,
            "enabled": True,
        }

        self.assertLess(
            controls.score_control_candidate(
                candidate,
                controls.CONTROL_SEND_BUTTON,
                {"x": 0, "y": 0, "width": 400, "height": 80},
            ),
            0,
        )
        self.assertFalse(
            controls.control_fingerprint_is_valid(
                controls.fingerprint_from_click(candidate["fingerprint"]),
                controls.CONTROL_SEND_BUTTON,
            )
        )

    def test_center_class_does_not_count_as_enter_send_semantics(self) -> None:
        candidate = {
            "fingerprint": {
                "tag": "div",
                "role": "button",
                "classes": ["model-select-item", "flex-y-center"],
            },
            "bottom_ratio": 0.6,
            "anchor_distance": 270,
            "enabled": True,
        }

        self.assertLess(
            controls.score_control_candidate(
                candidate,
                controls.CONTROL_SEND_BUTTON,
                {"x": 0, "y": 0, "width": 400, "height": 80},
            ),
            62,
        )

    def test_control_discovery_keeps_prior_transaction_markers_until_cleanup(self) -> None:
        source = controls.discovery._DISCOVER_CONTROLS_JS
        self.assertIn('[class~="enter"]', source)
        self.assertNotIn("removeAttribute('data-codey-auto-candidate')", source)
        self.assertIn("getAttribute('data-codey-auto-candidate')", source)
        self.assertIn(
            "removeAttribute('data-codey-auto-candidate')",
            controls.discovery._STOP_RESPONSE_WATCH_JS,
        )

    def test_response_discovery_keeps_locator_marker_until_transaction_cleanup(self) -> None:
        source = controls.discovery._READ_RESPONSE_WATCH_JS
        self.assertNotIn("removeAttribute('data-codey-response-candidate')", source)
        self.assertIn("getAttribute('data-codey-response-candidate')", source)
        self.assertIn("state.nextMarker++", source)
        self.assertIn(
            "removeAttribute('data-codey-response-candidate')",
            controls.discovery._STOP_RESPONSE_WATCH_JS,
        )

    def test_learned_control_is_verified_only_after_confirmation(self) -> None:
        page = mock.Mock()
        page.url = "https://chat.qwen.ai/"
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            controls.save_control(
                "qwen",
                controls.CONTROL_SEND_BUTTON,
                page,
                {"tag": "button", "aria_label": "Send"},
                path=path,
            )
            self.assertFalse(controls.load_control("qwen", controls.CONTROL_SEND_BUTTON, path=path)["verified"])

            controls._remember_source("qwen", controls.CONTROL_SEND_BUTTON, "learned")
            controls.confirm_control("qwen", controls.CONTROL_SEND_BUTTON, path=path)

            record = controls.load_control("qwen", controls.CONTROL_SEND_BUTTON, path=path)
            self.assertTrue(record["verified"])
            self.assertEqual(record["failures"], 0)

    def test_pending_confirmation_writes_verified_record_once(self) -> None:
        page = mock.Mock()
        page.url = "https://chat.qwen.ai/"
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            controls._remember_pending(
                "qwen",
                controls.CONTROL_SEND_BUTTON,
                page,
                {"tag": "button", "aria_label": "Send"},
            )
            controls._remember_source("qwen", controls.CONTROL_SEND_BUTTON, "pending")
            with mock.patch.object(
                controls,
                "write_json_atomic",
                wraps=controls.write_json_atomic,
            ) as write:
                controls.confirm_control("qwen", controls.CONTROL_SEND_BUTTON, path=path)

            record = controls.load_control("qwen", controls.CONTROL_SEND_BUTTON, path=path)
            self.assertTrue(record["verified"])
            self.assertEqual(record["failures"], 0)
            write.assert_called_once()

    def test_provider_send_stages_all_controls_until_success(self) -> None:
        page = mock.Mock(url="https://chat.qwen.ai/")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            controls._begin_revival_send("qwen", page)
            for action, fingerprint in (
                (controls.CONTROL_MESSAGE_BOX, {"tag": "textarea"}),
                (controls.CONTROL_SEND_BUTTON, {"tag": "button", "aria_label": "Send"}),
                (controls.CONTROL_RESPONSE, {"tag": "article", "classes": ["answer"]}),
            ):
                controls._remember_pending("qwen", action, page, fingerprint)
                controls._remember_source("qwen", action, "pending")
                controls.confirm_control("qwen", action, path=path)
            self.assertFalse(path.exists())

            controls._complete_revival_send("qwen")

            provider = controls.load_controls(path)["qwen"]
            self.assertEqual(provider["_revival"]["status"], "provisional")
            self.assertEqual(
                set(provider["_revival"]["verified_actions"]),
                {"message_box", "send_button", "response"},
            )

    def test_staged_control_locator_survives_until_send_transaction_completes(self) -> None:
        page = mock.Mock(url="https://chat.qwen.ai/")
        locator = mock.Mock()
        locator.is_visible.return_value = True
        locator.is_enabled.return_value = True
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            controls._begin_revival_send("qwen", page)
            controls._remember_pending(
                "qwen",
                controls.CONTROL_SEND_BUTTON,
                page,
                controls.fingerprint_from_click({"tag": "button", "role": "button"}),
                locator,
            )
            controls._remember_source(
                "qwen",
                controls.CONTROL_SEND_BUTTON,
                "pending",
            )

            controls.confirm_control(
                "qwen",
                controls.CONTROL_SEND_BUTTON,
                path=path,
            )

            self.assertEqual(
                controls._source_for("qwen", controls.CONTROL_SEND_BUTTON),
                "staged",
            )
            self.assertIs(
                controls.pending_control(
                    page,
                    "qwen",
                    controls.CONTROL_SEND_BUTTON,
                    require_enabled=True,
                ),
                locator,
            )

            controls._complete_revival_send("qwen")

            self.assertIsNone(
                controls.pending_control(
                    page,
                    "qwen",
                    controls.CONTROL_SEND_BUTTON,
                )
            )

    def test_rejected_staged_control_cannot_reuse_transaction_locator(self) -> None:
        page = mock.Mock(url="https://chat.qwen.ai/")
        locator = mock.Mock()
        locator.is_visible.return_value = True
        locator.is_enabled.return_value = True
        controls._begin_revival_send("qwen", page)
        controls._remember_pending(
            "qwen",
            controls.CONTROL_SEND_BUTTON,
            page,
            controls.fingerprint_from_click({"tag": "button", "role": "button"}),
            locator,
        )
        controls._remember_source(
            "qwen",
            controls.CONTROL_SEND_BUTTON,
            "pending",
        )
        controls.confirm_control("qwen", controls.CONTROL_SEND_BUTTON)

        controls.reject_control("qwen", controls.CONTROL_SEND_BUTTON)

        self.assertIsNone(
            controls.pending_control(
                page,
                "qwen",
                controls.CONTROL_SEND_BUTTON,
                require_enabled=True,
            )
        )
        self.assertEqual(
            controls._source_for("qwen", controls.CONTROL_SEND_BUTTON),
            "",
        )

    def test_failed_provider_send_discards_staged_controls(self) -> None:
        page = mock.Mock(url="https://chat.qwen.ai/")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            @controls.revival_send("qwen")
            def failing_send(current_page):
                controls._remember_pending(
                    "qwen",
                    controls.CONTROL_SEND_BUTTON,
                    current_page,
                    {"tag": "button"},
                )
                controls._remember_source(
                    "qwen", controls.CONTROL_SEND_BUTTON, "pending"
                )
                controls.confirm_control(
                    "qwen", controls.CONTROL_SEND_BUTTON, path=path
                )
                raise TimeoutError("network timeout")

            with self.assertRaisesRegex(TimeoutError, "network timeout"):
                failing_send(page)

            self.assertFalse(path.exists())
            self.assertNotIn("qwen", controls._revival_attempts())
            self.assertEqual(
                controls._source_for("qwen", controls.CONTROL_SEND_BUTTON), ""
            )

    def test_transient_failure_does_not_reduce_learned_bundle_health(self) -> None:
        page = mock.Mock(url="https://chat.qwen.ai/")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            controls.provider_revival.complete_send(
                path,
                "qwen",
                "chat.qwen.ai",
                {controls.CONTROL_RESPONSE: {"tag": "article", "classes": ["answer"]}},
                {controls.CONTROL_RESPONSE},
                set(),
            )
            before = controls.load_controls(path)

            @controls.revival_send("qwen")
            def timed_out(current_page):
                del current_page
                controls._remember_source(
                    "qwen", controls.CONTROL_RESPONSE, "learned"
                )
                controls.confirm_control(
                    "qwen", controls.CONTROL_RESPONSE, path=path
                )
                raise TimeoutError("provider timeout")

            with self.assertRaisesRegex(TimeoutError, "provider timeout"):
                timed_out(page)

            self.assertEqual(controls.load_controls(path), before)

    def test_flow_recovery_is_staged_then_promoted_by_next_natural_send(self) -> None:
        from codey import provider_flow

        page = mock.Mock(url="https://chat.qwen.ai/")
        observation = provider_flow.FlowObservation(
            response_stable=True,
            response_nonempty=True,
            stop_hidden=True,
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            trace = provider_flow.FlowTrace()
            trace.add(provider_flow.FlowObservation(stop_visible=True))
            trace.add(observation)
            trace.add(observation)
            provider_flow.begin_task_context("flow-session")

            @controls.revival_send("qwen")
            def recovered_send(current_page):
                return controls.flow_stage_ready(
                    current_page,
                    "qwen",
                    provider_flow.STAGE_COMPLETION,
                    trace,
                    observation,
                    built_in_ready=False,
                    allow_recovery=True,
                )

            @controls.revival_send("qwen")
            def natural_send(current_page):
                return controls.flow_stage_ready(
                    current_page,
                    "qwen",
                    provider_flow.STAGE_COMPLETION,
                    trace,
                    observation,
                    built_in_ready=True,
                )

            try:
                with mock.patch.object(
                    provider_flow,
                    "_handler",
                    lambda request: request.candidates[0].candidate_id,
                ), mock.patch.object(controls, "CONTROL_STORE", path):
                    self.assertTrue(recovered_send(page))
                    self.assertEqual(
                        controls.load_controls(path)["qwen"]["_revival"]["status"],
                        "provisional",
                    )
                    self.assertTrue(natural_send(page))
                    self.assertEqual(
                        controls.load_controls(path)["qwen"]["_revival"]["status"],
                        "active",
                    )
            finally:
                provider_flow.end_task_context()

    def test_failed_send_discards_staged_flow_without_writing(self) -> None:
        from codey import provider_flow

        page = mock.Mock(url="https://chat.qwen.ai/")
        observation = provider_flow.FlowObservation(
            response_stable=True,
            response_nonempty=True,
            stop_hidden=True,
        )
        trace = provider_flow.FlowTrace()
        trace.add(provider_flow.FlowObservation(stop_visible=True))
        trace.add(observation)
        trace.add(observation)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            provider_flow.begin_task_context("flow-session")

            @controls.revival_send("qwen")
            def failing_send(current_page):
                self.assertTrue(
                    controls.flow_stage_ready(
                        current_page,
                        "qwen",
                        provider_flow.STAGE_COMPLETION,
                        trace,
                        observation,
                        built_in_ready=False,
                        allow_recovery=True,
                    )
                )
                raise TimeoutError("response read failed")

            try:
                with (
                    mock.patch.object(
                        provider_flow,
                        "_handler",
                        lambda request: request.candidates[0].candidate_id,
                    ),
                    mock.patch.object(controls, "CONTROL_STORE", path),
                    self.assertRaisesRegex(TimeoutError, "response read failed"),
                ):
                    failing_send(page)
                self.assertFalse(path.exists())
            finally:
                provider_flow.end_task_context()

    def test_persisted_flow_unreadable_final_response_triggers_rollback(self) -> None:
        from codey import provider_flow, provider_revival
        from codey.provider_diagnostics import ResponseMissing

        page = mock.Mock(url="https://chat.qwen.ai/")
        generating = provider_flow.FlowObservation(stop_visible=True)
        terminal = provider_flow.FlowObservation(
            response_stable=True,
            response_nonempty=True,
            stop_hidden=True,
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            profile_digest = provider_flow.profile_hash(controls.get_profile("qwen"))
            provider_revival.complete_send(
                path,
                "qwen",
                "chat.qwen.ai",
                {},
                set(),
                set(),
                staged_flow={
                    provider_flow.STAGE_COMPLETION: (
                        provider_flow.PREDICATE_RESPONSE_STABLE,
                        provider_flow.PREDICATE_STOP_HIDDEN,
                    )
                },
                built_in_profile_hash=profile_digest,
            )
            provider_revival.complete_send(
                path,
                "qwen",
                "chat.qwen.ai",
                {},
                set(),
                set(),
                learned_flow_verified=True,
                built_in_profile_hash=profile_digest,
            )
            controls.begin_task_context("rollback-session")

            @controls.revival_send("qwen")
            def structurally_failing_send(current_page):
                trace = provider_flow.FlowTrace()
                trace.add(generating)
                trace.add(terminal)
                trace.add(terminal)
                self.assertTrue(
                    controls.flow_stage_ready(
                        current_page,
                        "qwen",
                        provider_flow.STAGE_COMPLETION,
                        trace,
                        terminal,
                        built_in_ready=False,
                    )
                )
                return controls.read_flow_response(
                    "qwen",
                    provider_flow.STAGE_COMPLETION,
                    lambda: (_ for _ in ()).throw(
                        RuntimeError("Could not read the Qwen Studio response")
                    ),
                )

            with mock.patch.object(controls, "CONTROL_STORE", path):
                for expected in (1, None):
                    with self.assertRaises(ResponseMissing):
                        structurally_failing_send(page)
                    meta = controls.load_controls(path)["qwen"].get("_revival")
                    if expected is None:
                        self.assertIsNone(meta)
                    else:
                        self.assertEqual(meta["failures"], expected)

    def test_builtin_completion_read_failure_is_not_attributed_to_flow(self) -> None:
        from codey import provider_flow, provider_revival

        page = mock.Mock(url="https://chat.qwen.ai/")
        generating = provider_flow.FlowObservation(stop_visible=True)
        terminal = provider_flow.FlowObservation(
            response_stable=True,
            response_nonempty=True,
            stop_hidden=True,
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            profile_digest = provider_flow.profile_hash(controls.get_profile("qwen"))
            provider_revival.complete_send(
                path,
                "qwen",
                "chat.qwen.ai",
                {},
                set(),
                set(),
                staged_flow={
                    provider_flow.STAGE_COMPLETION: (
                        provider_flow.PREDICATE_RESPONSE_STABLE,
                        provider_flow.PREDICATE_STOP_HIDDEN,
                    )
                },
                built_in_profile_hash=profile_digest,
            )
            before = path.read_bytes()
            controls.begin_task_context("builtin-session")

            @controls.revival_send("qwen")
            def builtin_send(current_page):
                trace = provider_flow.FlowTrace()
                trace.add(generating)
                trace.add(terminal)
                trace.add(terminal)
                self.assertTrue(
                    controls.flow_stage_ready(
                        current_page,
                        "qwen",
                        provider_flow.STAGE_COMPLETION,
                        trace,
                        terminal,
                        built_in_ready=True,
                    )
                )
                return controls.read_flow_response(
                    "qwen",
                    provider_flow.STAGE_COMPLETION,
                    lambda: (_ for _ in ()).throw(RuntimeError("read failed")),
                )

            with (
                mock.patch.object(controls, "CONTROL_STORE", path),
                self.assertRaisesRegex(RuntimeError, "read failed"),
            ):
                builtin_send(page)

            self.assertEqual(path.read_bytes(), before)

    def test_control_and_flow_failure_are_counted_once_per_send(self) -> None:
        from codey import provider_flow, provider_revival
        from codey.provider_diagnostics import ResponseMissing

        page = mock.Mock(url="https://chat.qwen.ai/")
        terminal = provider_flow.FlowObservation(
            response_stable=True,
            response_nonempty=True,
            stop_hidden=True,
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            profile_digest = provider_flow.profile_hash(controls.get_profile("qwen"))
            provider_revival.complete_send(
                path,
                "qwen",
                "chat.qwen.ai",
                {controls.CONTROL_RESPONSE: {"tag": "article", "classes": ["answer"]}},
                {controls.CONTROL_RESPONSE},
                set(),
                staged_flow={
                    provider_flow.STAGE_COMPLETION: (
                        provider_flow.PREDICATE_RESPONSE_STABLE,
                        provider_flow.PREDICATE_STOP_HIDDEN,
                    )
                },
                built_in_profile_hash=profile_digest,
            )
            provider_revival.complete_send(
                path,
                "qwen",
                "chat.qwen.ai",
                {},
                {controls.CONTROL_RESPONSE},
                {controls.CONTROL_RESPONSE},
                learned_flow_verified=True,
                built_in_profile_hash=profile_digest,
            )
            controls.begin_task_context("dedupe-session")

            @controls.revival_send("qwen")
            def unreadable_send(current_page):
                trace = provider_flow.FlowTrace()
                trace.add(provider_flow.FlowObservation(stop_visible=True))
                trace.add(terminal)
                trace.add(terminal)
                self.assertTrue(
                    controls.flow_stage_ready(
                        current_page,
                        "qwen",
                        provider_flow.STAGE_COMPLETION,
                        trace,
                        terminal,
                        built_in_ready=False,
                    )
                )
                controls._remember_source(
                    "qwen",
                    controls.CONTROL_RESPONSE,
                    "learned",
                )
                controls.reject_control(
                    "qwen",
                    controls.CONTROL_RESPONSE,
                    path=path,
                )
                return controls.read_flow_response(
                    "qwen",
                    provider_flow.STAGE_COMPLETION,
                    lambda: (_ for _ in ()).throw(RuntimeError("unreadable")),
                )

            with (
                mock.patch.object(controls, "CONTROL_STORE", path),
                self.assertRaises(ResponseMissing),
            ):
                unreadable_send(page)

            provider = controls.load_controls(path)["qwen"]

        self.assertEqual(provider["_revival"]["failures"], 1)
        self.assertEqual(provider[controls.CONTROL_RESPONSE]["failures"], 1)

    def test_transient_failure_does_not_penalize_persisted_flow(self) -> None:
        from codey import provider_flow, provider_revival
        from codey.provider_submission import SubmissionUncertain

        page = mock.Mock(url="https://chat.qwen.ai/")
        generating = provider_flow.FlowObservation(stop_visible=True)
        terminal = provider_flow.FlowObservation(
            response_stable=True,
            response_nonempty=True,
            stop_hidden=True,
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            profile_digest = provider_flow.profile_hash(controls.get_profile("qwen"))
            provider_revival.complete_send(
                path,
                "qwen",
                "chat.qwen.ai",
                {},
                set(),
                set(),
                staged_flow={
                    provider_flow.STAGE_COMPLETION: (
                        provider_flow.PREDICATE_RESPONSE_STABLE,
                        provider_flow.PREDICATE_STOP_HIDDEN,
                    )
                },
                built_in_profile_hash=profile_digest,
            )
            before = path.read_bytes()
            controls.begin_task_context("transient-session")

            @controls.revival_send("qwen")
            def failing_send(current_page, error):
                trace = provider_flow.FlowTrace()
                trace.add(generating)
                trace.add(terminal)
                trace.add(terminal)
                self.assertTrue(
                    controls.flow_stage_ready(
                        current_page,
                        "qwen",
                        provider_flow.STAGE_COMPLETION,
                        trace,
                        terminal,
                        built_in_ready=False,
                    )
                )
                raise error

            with mock.patch.object(controls, "CONTROL_STORE", path):
                for error in (
                    TimeoutError("network stalled"),
                    SubmissionUncertain("submission uncertain"),
                ):
                    with self.subTest(error=type(error).__name__):
                        with self.assertRaises(type(error)):
                            failing_send(page, error)

            self.assertEqual(path.read_bytes(), before)

    def test_missing_flow_is_read_once_per_task_and_invalidated_on_commit(self) -> None:
        from codey import provider_flow

        page = mock.Mock(url="https://chat.qwen.ai/")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            controls.begin_task_context("cache-session")

            @controls.revival_send("qwen")
            def healthy_send(_current_page):
                return "ok"

            with (
                mock.patch.object(controls, "CONTROL_STORE", path),
                mock.patch.object(
                    controls.provider_revival,
                    "load_flow_recipe",
                    return_value=None,
                ) as load,
            ):
                self.assertEqual(healthy_send(page), "ok")
                self.assertEqual(healthy_send(page), "ok")
                self.assertEqual(load.call_count, 1)

                attempt = controls._RevivalSend(
                    host="chat.qwen.ai",
                    staged={},
                    verified=set(),
                    learned_verified=set(),
                    profile_hash=provider_flow.profile_hash(
                        controls.get_profile("qwen")
                    ),
                    staged_flow={
                        provider_flow.STAGE_COMPLETION: (
                            provider_flow.PREDICATE_RESPONSE_STABLE,
                            provider_flow.PREDICATE_STOP_HIDDEN,
                        )
                    },
                )
                controls._revival_attempts()["qwen"] = attempt
                controls._complete_revival_send("qwen")
                self.assertEqual(healthy_send(page), "ok")
                self.assertEqual(load.call_count, 2)

    def test_pending_confirmation_ignores_storage_failure(self) -> None:
        page = mock.Mock()
        page.url = "https://chat.qwen.ai/"
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            controls._remember_pending(
                "qwen",
                controls.CONTROL_SEND_BUTTON,
                page,
                {"tag": "button", "aria_label": "Send"},
            )
            controls._remember_source("qwen", controls.CONTROL_SEND_BUTTON, "pending")

            with mock.patch.object(
                controls,
                "write_json_atomic",
                side_effect=OSError("disk full"),
            ):
                controls.confirm_control("qwen", controls.CONTROL_SEND_BUTTON, path=path)

            self.assertIsNone(controls.load_control("qwen", controls.CONTROL_SEND_BUTTON, path=path))
            self.assertEqual(
                controls._source_for("qwen", controls.CONTROL_SEND_BUTTON),
                "",
            )

    def test_learned_control_is_forgotten_after_two_failed_validations(self) -> None:
        page = mock.Mock()
        page.url = "https://chat.qwen.ai/"
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            controls.save_control(
                "qwen",
                controls.CONTROL_SEND_BUTTON,
                page,
                {"tag": "button", "aria_label": "Send"},
                path=path,
            )
            controls._remember_source("qwen", controls.CONTROL_SEND_BUTTON, "learned")
            controls.reject_control("qwen", controls.CONTROL_SEND_BUTTON, path=path)
            self.assertIsNotNone(controls.load_control("qwen", controls.CONTROL_SEND_BUTTON, path=path))

            controls.reject_control("qwen", controls.CONTROL_SEND_BUTTON, path=path)

            self.assertIsNone(controls.load_control("qwen", controls.CONTROL_SEND_BUTTON, path=path))

    def test_response_scoring_prefers_assistant_markdown_over_user_content(self) -> None:
        assistant = {
            "text": '{"tool":"done"}',
            "fingerprint": {"tag": "div", "classes": ["assistant", "markdown-prose"]},
            "bottom_ratio": 0.7,
        }
        user = {
            "text": "please fix this",
            "fingerprint": {"tag": "div", "classes": ["user-message"]},
            "bottom_ratio": 0.7,
        }

        self.assertGreater(
            controls.score_response_candidate(assistant),
            controls.score_response_candidate(user),
        )

    def test_new_markdown_region_reaches_response_safety_threshold(self) -> None:
        candidate = {
            "text": "RECOVERY_OK",
            "fingerprint": {"tag": "div", "classes": ["markdown-prose"]},
            "bottom_ratio": 0.7,
        }

        self.assertGreaterEqual(controls.score_response_candidate(candidate), 48)

    def test_discovered_response_is_saved_only_after_success(self) -> None:
        page = mock.Mock()
        page.url = "https://chat.qwen.ai/"
        candidate = mock.Mock()
        candidate.is_visible.return_value = True
        locator = mock.Mock()
        locator.count.return_value = 1
        locator.nth.return_value = candidate
        page.locator.return_value = locator
        page.evaluate.side_effect = [
            None,
            [{
                "selector": '[data-codey-response-candidate="one"]',
                "visible": True,
                "text": '{"tool":"done"}',
                "bottom_ratio": 0.8,
                "fingerprint": {
                    "tag": "div",
                    "classes": ["assistant", "markdown-prose"],
                },
            }],
        ]
        controls.start_response_watch(page, "qwen")
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            with mock.patch.object(controls, "CONTROL_STORE", path):
                response = controls.discover_response(page, "qwen")
                self.assertIs(response, candidate)
                self.assertIsNone(controls.load_control("qwen", controls.CONTROL_RESPONSE, path=path))

                controls.confirm_control("qwen", controls.CONTROL_RESPONSE, path=path)

                record = controls.load_control("qwen", controls.CONTROL_RESPONSE, path=path)
                self.assertTrue(record["verified"])

    def test_response_locator_is_reused_during_the_same_transaction(self) -> None:
        page = mock.Mock()
        response = mock.Mock()
        response.is_visible.return_value = True
        controls._response_locator_map()["qwen"] = response
        try:
            with mock.patch.object(controls, "visible_locator", return_value=None):
                result = controls.locate_response(page, "qwen", (".missing",))
        finally:
            controls._response_locator_map().pop("qwen", None)

        self.assertIs(result, response)

    def test_send_button_rejects_upload_controls(self) -> None:
        fingerprint = controls.fingerprint_from_click({
            "tag": "button",
            "role": "button",
            "ariaLabel": "Upload file",
            "text": "",
        })

        self.assertFalse(
            controls.control_fingerprint_is_valid(
                fingerprint,
                controls.CONTROL_SEND_BUTTON,
            )
        )

    def test_message_box_accepts_textarea_and_contenteditable(self) -> None:
        textarea = controls.fingerprint_from_click({"tag": "textarea"})
        editable = controls.fingerprint_from_click({"tag": "div", "contentEditable": True})

        self.assertTrue(controls.control_fingerprint_is_valid(textarea, controls.CONTROL_MESSAGE_BOX))
        self.assertTrue(controls.control_fingerprint_is_valid(editable, controls.CONTROL_MESSAGE_BOX))

    def test_message_box_rejects_password_and_file_inputs(self) -> None:
        for input_type in ("password", "file"):
            fingerprint = controls.fingerprint_from_click({
                "tag": "input",
                "type": input_type,
                "placeholder": "Message",
            })
            self.assertFalse(
                controls.control_fingerprint_is_valid(
                    fingerprint, controls.CONTROL_MESSAGE_BOX
                )
            )

    def test_discovery_does_not_run_on_wrong_provider_host(self) -> None:
        page = mock.Mock(url="https://accounts.example.com/login")
        with mock.patch.object(controls.discovery, "control_candidates") as candidates:
            result = controls.discover_control(
                page,
                "qwen",
                controls.CONTROL_MESSAGE_BOX,
            )

        self.assertIsNone(result)
        candidates.assert_not_called()

    def test_request_teaching_calls_registered_handler_with_session(self) -> None:
        page = mock.Mock(url="https://chat.qwen.ai/")
        handler = mock.Mock(return_value="control")
        controls.begin_task_context("session-1")
        controls.set_teach_handler(handler)

        result = controls.request_teaching(
            page,
            "qwen",
            controls.CONTROL_SEND_BUTTON,
            require_enabled=True,
        )

        self.assertEqual(result, "control")
        request = handler.call_args.args[0]
        self.assertEqual(request.provider_id, "qwen")
        self.assertEqual(request.action, controls.CONTROL_SEND_BUTTON)
        self.assertEqual(request.session_id, "session-1")
        self.assertEqual(request.message, "Click the send button in the model page")

    def test_doctor_selects_ambiguous_candidate_then_existing_confirmation_saves_it(self) -> None:
        page = mock.Mock(url="https://chat.qwen.ai/")
        first = mock.Mock()
        second = mock.Mock()
        first.is_enabled.return_value = True
        second.is_enabled.return_value = True
        candidates = (
            controls.discovery.Discovery(
                first,
                {"tag": "button", "ariaLabel": "Send message"},
                70,
            ),
            controls.discovery.Discovery(
                second,
                {"tag": "button", "ariaLabel": "Submit"},
                69,
            ),
        )
        controls.begin_task_context("session-1")
        doctor = mock.Mock(return_value="c2")
        controls.set_doctor_handler(doctor)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            with (
                mock.patch.object(controls.discovery, "control_candidates", return_value=candidates),
                mock.patch.object(controls.discovery, "select_control_candidate", return_value=None),
            ):
                result = controls.discover_control(
                    page,
                    "qwen",
                    controls.CONTROL_SEND_BUTTON,
                    require_enabled=True,
                )
                controls.confirm_control("qwen", controls.CONTROL_SEND_BUTTON, path=path)

            record = controls.load_control("qwen", controls.CONTROL_SEND_BUTTON, path=path)

        self.assertIs(result, second)
        doctor.assert_called_once()
        self.assertEqual(record["fingerprint"]["aria_label"], "Submit")
        self.assertTrue(record["verified"])

    def test_doctor_runs_once_per_action_and_cannot_recurse(self) -> None:
        page = mock.Mock(url="https://chat.qwen.ai/")
        locator = mock.Mock()
        locator.is_enabled.return_value = True
        candidates = (
            controls.discovery.Discovery(
                locator,
                {"tag": "button", "ariaLabel": "Send"},
                50,
            ),
        )
        calls = []

        def doctor(_request):
            calls.append((controls.can_doctor(), controls.can_teach()))
            return None

        controls.begin_task_context("session-1")
        controls.set_doctor_handler(doctor)
        controls.set_teach_handler(mock.Mock())
        with (
            mock.patch.object(controls.discovery, "control_candidates", return_value=candidates),
            mock.patch.object(controls.discovery, "select_control_candidate", return_value=None),
        ):
            controls.discover_control(page, "qwen", controls.CONTROL_SEND_BUTTON)
            controls.discover_control(page, "qwen", controls.CONTROL_SEND_BUTTON)

        self.assertEqual(calls, [(False, False)])

    def test_manual_teaching_follows_failed_doctor(self) -> None:
        page = mock.Mock(url="https://chat.qwen.ai/")
        locator = mock.Mock()
        candidates = (
            controls.discovery.Discovery(
                locator,
                {"tag": "textarea", "placeholder": "Message"},
                50,
            ),
        )
        order = []
        controls.begin_task_context("session-1")
        controls.set_doctor_handler(lambda _request: order.append("doctor"))
        controls.set_teach_handler(lambda _request: order.append("manual") or "taught")
        with (
            mock.patch.object(controls.discovery, "control_candidates", return_value=candidates),
            mock.patch.object(controls.discovery, "select_control_candidate", return_value=None),
        ):
            result = controls.locate_control(
                page,
                "qwen",
                controls.CONTROL_MESSAGE_BOX,
                (),
                teach=True,
            )

        self.assertEqual(result, "taught")
        self.assertEqual(order, ["doctor", "manual"])

    def test_response_doctor_waits_for_explicit_timeout_recovery(self) -> None:
        page = mock.Mock(url="https://chat.qwen.ai/")
        response = mock.Mock()
        response.is_visible.return_value = True
        candidate = controls.discovery.Discovery(
            response,
            {"tag": "article", "classes": ["answer"]},
            40,
        )
        doctor = mock.Mock(return_value="c1")
        controls.begin_task_context("session-1")
        controls.set_doctor_handler(doctor)
        controls._context.response_watches = {"qwen": "watch"}
        with (
            mock.patch.object(controls.discovery, "response_candidates", return_value=(candidate,)),
            mock.patch.object(controls.discovery, "select_response_candidate", return_value=None),
        ):
            self.assertIsNone(controls.discover_response(page, "qwen"))
            selected = controls.request_doctor_response(page, "qwen")

        self.assertIs(selected, response)
        doctor.assert_called_once()

    def test_failed_doctor_response_validation_falls_through_to_teaching(self) -> None:
        read = mock.Mock(side_effect=[ValueError("detached"), "answer"])
        with (
            mock.patch.object(controls, "request_doctor_response", return_value=mock.Mock()),
            mock.patch.object(controls, "teach_response", return_value=mock.Mock()),
            mock.patch.object(controls, "reject_control") as reject,
        ):
            answer = controls.recover_response(mock.Mock(), "qwen", read)

        self.assertEqual(answer, "answer")
        self.assertEqual(read.call_count, 2)
        reject.assert_called_once_with("qwen", controls.CONTROL_RESPONSE)

    def test_response_fingerprint_rejects_interactive_control(self) -> None:
        self.assertFalse(
            controls.control_fingerprint_is_valid(
                controls.fingerprint_from_click({"tag": "button", "text": "Copy"}),
                controls.CONTROL_RESPONSE,
            )
        )


if __name__ == "__main__":
    unittest.main()
