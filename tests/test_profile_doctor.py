from __future__ import annotations

import json
import unittest
from unittest import mock

from codey.providers import profile_doctor
from codey.providers.discovery import Discovery


class ProfileDoctorTests(unittest.TestCase):
    def _request(self):
        candidates = (
            Discovery(
                locator=object(),
                fingerprint={
                    "tag": "button",
                    "role": "button",
                    "ariaLabel": "Send",
                    "classes": ["primary", "filled", "_52c986b"],
                    "data": {"data-testid": "composer-send"},
                },
                score=57,
                metadata={"enabled": True, "anchor_distance": 30, "bottom_ratio": 0.9},
            ),
            Discovery(
                locator=object(),
                fingerprint={"tag": "button", "role": "button", "ariaLabel": "Upload"},
                score=56,
                metadata={"enabled": True, "anchor_distance": 35, "bottom_ratio": 0.9},
            ),
        )
        return profile_doctor.make_request("stepfun", "send_button", object(), candidates)

    def test_valid_known_candidate_is_selected_with_one_call(self) -> None:
        request = self._request()
        send = mock.Mock(return_value='{"candidate_id":"c1"}')

        selected = profile_doctor.choose_candidate(request, send)

        self.assertEqual(selected, "c1")
        send.assert_called_once()

    def test_invalid_or_decorated_reply_is_rejected_without_repair_call(self) -> None:
        request = self._request()
        for reply in (
            '```json\n{"candidate_id":"c1"}\n```',
            '{"candidate_id":"c9"}',
            '{"candidate_id":"c1","reason":"looks right"}',
        ):
            with self.subTest(reply=reply):
                send = mock.Mock(return_value=reply)
                self.assertIsNone(profile_doctor.choose_candidate(request, send))
                send.assert_called_once()

    def test_provider_status_prefix_does_not_trigger_a_repair_call(self) -> None:
        request = self._request()
        send = mock.Mock(return_value='已深度思考（用时 4.9 秒）\n\n{"candidate_id":"c1"}')

        selected = profile_doctor.choose_candidate(request, send)

        self.assertEqual(selected, "c1")
        send.assert_called_once()

    def test_prompt_contains_only_sanitized_bounded_structure(self) -> None:
        secret = "sk-abcdefghijklmnopqrstuvwxyz C:\\Users\\Alice test@example.com"
        discovery = Discovery(
            locator=object(),
            fingerprint={
                "tag": "textarea",
                "placeholder": secret,
                "text": "private project source",
                "classes": ["message-box"],
                "data": {"data-testid": "chat-input", "data-private": secret},
            },
            score=55,
            metadata={"area": 2000, "bottom_ratio": 0.8},
        )
        request = profile_doctor.make_request("qwen", "message_box", object(), (discovery,))

        prompt = profile_doctor.render_prompt(request)

        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", prompt)
        self.assertNotIn("Users", prompt)
        self.assertNotIn("example.com", prompt)
        self.assertNotIn("private project source", prompt)
        self.assertNotIn("data-private", prompt)
        payload = json.loads(prompt.split("\n", 1)[1])
        self.assertEqual(payload["candidates"][0]["label"], "")

    def test_prompt_never_includes_arbitrary_visible_control_text(self) -> None:
        discovery = Discovery(
            locator=object(),
            fingerprint={"tag": "button", "text": "Send Acme confidential pricing plan"},
            score=61,
        )
        request = profile_doctor.make_request("qwen", "send_button", object(), (discovery,))

        prompt = profile_doctor.render_prompt(request)

        self.assertIn('\"label\":\"send\"', prompt)
        self.assertNotIn("Acme", prompt)
        self.assertNotIn("confidential pricing", prompt)

    def test_prompt_whitelists_structural_enums(self) -> None:
        discovery = Discovery(
            locator=object(),
            fingerprint={
                "tag": "x-acme-private",
                "role": "client-secret-role",
                "type": "project-orchid",
            },
            score=40,
        )
        request = profile_doctor.make_request("qwen", "message_box", object(), (discovery,))

        prompt = profile_doctor.render_prompt(request)
        candidate = json.loads(prompt.split("\n", 1)[1])["candidates"][0]

        self.assertEqual(candidate["tag"], "other")
        self.assertEqual(candidate["role"], "other")
        self.assertEqual(candidate["input_type"], "other")
        self.assertNotIn("acme", prompt)
        self.assertNotIn("client-secret", prompt)
        self.assertNotIn("project-orchid", prompt)

    def test_response_summary_never_contains_response_text(self) -> None:
        discovery = Discovery(
            locator=object(),
            fingerprint={"tag": "div", "text": "private model answer", "classes": ["markdown-prose"]},
            score=41,
        )
        request = profile_doctor.make_request("stepfun", "response", object(), (discovery,))

        prompt = profile_doctor.render_prompt(request)

        self.assertNotIn("private model answer", prompt)
        self.assertIn("markdown,prose", prompt)

    def test_center_class_does_not_emit_enter_structure_hint(self) -> None:
        self.assertNotIn("enter", profile_doctor._structure_hint("flex-y-center").split(","))

    def test_exact_enter_class_emits_enter_structure_hint(self) -> None:
        self.assertIn("enter", profile_doctor._structure_hint("enter").split(","))


if __name__ == "__main__":
    unittest.main()