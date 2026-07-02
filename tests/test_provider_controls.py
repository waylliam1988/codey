from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey import provider_controls as controls


class ProviderControlsTests(unittest.TestCase):
    def tearDown(self) -> None:
        controls.set_teach_handler(None)
        controls.set_session_id("")

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

    def test_control_discovery_keeps_prior_transaction_markers_until_cleanup(self) -> None:
        source = controls.discovery._DISCOVER_CONTROLS_JS
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

    def test_request_teaching_calls_registered_handler_with_session(self) -> None:
        page = mock.Mock()
        handler = mock.Mock(return_value="control")
        controls.set_session_id("session-1")
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


if __name__ == "__main__":
    unittest.main()
