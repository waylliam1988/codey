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
