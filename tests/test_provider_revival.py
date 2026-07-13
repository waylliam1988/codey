from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey import provider_revival as revival
from codey.local_store import read_json, write_json_atomic


class ProviderRevivalTests(unittest.TestCase):
    def test_normal_profile_only_send_does_not_touch_local_store(self) -> None:
        with mock.patch.object(revival, "read_json") as read:
            revival.complete_send(
                Path("unused.json"),
                "qwen",
                "chat.qwen.ai",
                {},
                {"message_box", "send_button", "response"},
                set(),
            )

        read.assert_not_called()

    def test_staged_controls_are_committed_in_one_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            staged = {
                "message_box": {"tag": "textarea", "placeholder": "Ask"},
                "send_button": {"tag": "button", "aria_label": "Send"},
                "response": {"tag": "article", "classes": ["answer"]},
            }
            with mock.patch.object(
                revival,
                "write_json_atomic",
                wraps=write_json_atomic,
            ) as write:
                revival.complete_send(
                    path,
                    "qwen",
                    "chat.qwen.ai",
                    staged,
                    set(staged),
                    set(),
                )

            data = read_json(path)
            self.assertEqual(write.call_count, 1)
            self.assertEqual(data["qwen"]["_revival"]["status"], "provisional")
            self.assertEqual(data["qwen"]["_revival"]["success_count"], 1)
            for action in staged:
                self.assertTrue(data["qwen"][action]["verified"])

    def test_second_independent_use_promotes_provisional_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            staged = {"send_button": {"tag": "button", "aria_label": "Send"}}
            revival.complete_send(
                path, "qwen", "chat.qwen.ai", staged, {"send_button"}, set()
            )
            revival.complete_send(
                path, "qwen", "chat.qwen.ai", {}, {"send_button"}, {"send_button"}
            )

            meta = read_json(path)["qwen"]["_revival"]

        self.assertEqual(meta["status"], "active")
        self.assertEqual(meta["success_count"], 2)

    def test_profile_control_does_not_promote_unused_override(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            staged = {"response": {"tag": "article", "classes": ["answer"]}}
            revival.complete_send(
                path, "qwen", "chat.qwen.ai", staged, {"response"}, set()
            )
            revival.complete_send(
                path, "qwen", "chat.qwen.ai", {}, {"response"}, set()
            )

            meta = read_json(path)["qwen"]["_revival"]

        self.assertEqual(meta["status"], "provisional")
        self.assertEqual(meta["success_count"], 1)

    def test_full_active_success_clears_prior_bundle_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            staged = {"send_button": {"tag": "button", "aria_label": "Send"}}
            revival.complete_send(
                path, "qwen", "chat.qwen.ai", staged, {"send_button"}, set()
            )
            revival.complete_send(
                path, "qwen", "chat.qwen.ai", {},
                {"send_button"}, {"send_button"},
            )
            revival.record_control_failure(path, "qwen", "send_button")

            revival.complete_send(
                path, "qwen", "chat.qwen.ai", {},
                {"send_button"}, {"send_button"},
            )
            provider = read_json(path)["qwen"]

        self.assertEqual(provider["_revival"]["failures"], 0)
        self.assertEqual(provider["send_button"]["failures"], 0)

    def test_stable_active_success_does_not_rewrite_store(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            staged = {"send_button": {"tag": "button", "aria_label": "Send"}}
            revival.complete_send(
                path, "qwen", "chat.qwen.ai", staged, {"send_button"}, set()
            )
            revival.complete_send(
                path, "qwen", "chat.qwen.ai", {},
                {"send_button"}, {"send_button"},
            )

            with mock.patch.object(revival, "write_json_atomic") as write:
                revival.complete_send(
                    path, "qwen", "chat.qwen.ai", {},
                    {"send_button"}, {"send_button"},
                )

        write.assert_not_called()

    def test_two_explicit_control_failures_restore_previous_bundle(self) -> None:
        old = {
            "host": "chat.qwen.ai",
            "fingerprint": {"tag": "button", "aria_label": "Old send"},
            "verified": True,
            "failures": 0,
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            write_json_atomic(path, {"qwen": {"send_button": old}})
            revival.complete_send(
                path,
                "qwen",
                "chat.qwen.ai",
                {"send_button": {"tag": "button", "aria_label": "New send"}},
                {"send_button"},
                set(),
            )
            revival.record_control_failure(path, "qwen", "send_button")
            revival.record_control_failure(path, "qwen", "send_button")

            provider = read_json(path)["qwen"]

        self.assertEqual(provider["send_button"], old)
        self.assertNotIn("_revival", provider)

    def test_rollback_restores_previous_active_generation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            old = {"tag": "button", "aria_label": "Old send"}
            revival.complete_send(
                path, "qwen", "chat.qwen.ai", {"send_button": old},
                {"send_button"}, set(),
            )
            revival.complete_send(
                path, "qwen", "chat.qwen.ai", {},
                {"send_button"}, {"send_button"},
            )
            revival.complete_send(
                path,
                "qwen",
                "chat.qwen.ai",
                {"send_button": {"tag": "button", "aria_label": "New send"}},
                {"send_button"},
                set(),
            )
            revival.record_control_failure(path, "qwen", "send_button")
            revival.record_control_failure(path, "qwen", "send_button")

            provider = read_json(path)["qwen"]

        self.assertEqual(provider["send_button"]["fingerprint"], old)
        self.assertEqual(provider["_revival"]["status"], "active")
        self.assertNotIn("previous_bundle", provider["_revival"])

    def test_rollback_does_not_restore_unmodified_control_deleted_later(self) -> None:
        old_message = {
            "host": "chat.qwen.ai",
            "fingerprint": {"tag": "textarea", "placeholder": "Old"},
            "verified": True,
            "failures": 0,
        }
        old_send = {
            "host": "chat.qwen.ai",
            "fingerprint": {"tag": "button", "aria_label": "Old send"},
            "verified": True,
            "failures": 0,
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            write_json_atomic(path, {
                "qwen": {
                    "message_box": old_message,
                    "send_button": old_send,
                }
            })
            revival.complete_send(
                path,
                "qwen",
                "chat.qwen.ai",
                {"send_button": {"tag": "button", "aria_label": "New send"}},
                {"send_button"},
                set(),
            )
            data = read_json(path)
            data["qwen"].pop("message_box")
            write_json_atomic(path, data)

            revival.record_control_failure(path, "qwen", "send_button")
            revival.record_control_failure(path, "qwen", "send_button")
            provider = read_json(path)["qwen"]

        self.assertNotIn("message_box", provider)
        self.assertEqual(provider["send_button"], old_send)

    def test_rollback_drops_previous_metadata_when_its_control_was_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            provider_revival_message = {"tag": "textarea", "placeholder": "Old"}
            revival.complete_send(
                path,
                "qwen",
                "chat.qwen.ai",
                {"message_box": provider_revival_message},
                {"message_box"},
                set(),
            )
            revival.complete_send(
                path,
                "qwen",
                "chat.qwen.ai",
                {},
                {"message_box"},
                {"message_box"},
            )
            revival.complete_send(
                path,
                "qwen",
                "chat.qwen.ai",
                {"send_button": {"tag": "button", "aria_label": "New send"}},
                {"send_button"},
                set(),
            )
            data = read_json(path)
            data["qwen"].pop("message_box")
            write_json_atomic(path, data)

            revival.record_control_failure(path, "qwen", "send_button")
            revival.record_control_failure(path, "qwen", "send_button")
            provider = read_json(path)["qwen"]

        self.assertNotIn("message_box", provider)
        self.assertNotIn("_revival", provider)

    def test_failure_of_unrelated_control_does_not_rollback_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "controls.json"
            write_json_atomic(path, {
                "qwen": {
                    "message_box": {
                        "host": "chat.qwen.ai",
                        "fingerprint": {"tag": "textarea"},
                        "verified": True,
                        "failures": 0,
                    }
                }
            })
            revival.complete_send(
                path,
                "qwen",
                "chat.qwen.ai",
                {"send_button": {"tag": "button", "aria_label": "Send"}},
                {"send_button"},
                set(),
            )
            revival.record_control_failure(path, "qwen", "message_box")

            provider = read_json(path)["qwen"]

        self.assertEqual(provider["_revival"]["failures"], 0)
        self.assertEqual(provider["message_box"]["failures"], 1)

if __name__ == "__main__":
    unittest.main()
