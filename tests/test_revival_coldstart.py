"""Cold-start: revival meta no longer reads legacy ``actions``.

Current writers only emit ``changed_actions`` / ``required_actions``
(revival.py). The old ``actions`` fallback is historical local-state compat,
not runtime tolerance for web/DOM changes, and must not silently revive
legacy bundles on a cold start.
"""

from __future__ import annotations

import unittest

from codey.providers import revival as revival_module


class RevivalColdStartMetaTests(unittest.TestCase):
    def test_legacy_actions_field_is_ignored(self) -> None:
        legacy = {"actions": ["send_button", "response"]}
        self.assertEqual(revival_module._changed_actions(legacy), set())
        self.assertEqual(revival_module._required_actions(legacy), set())

    def test_canonical_fields_still_win(self) -> None:
        meta = {
            "changed_actions": ["send_button"],
            "required_actions": ["send_button", "response"],
            "actions": ["message_box"],
        }
        self.assertEqual(revival_module._changed_actions(meta), {"send_button"})
        self.assertEqual(
            revival_module._required_actions(meta), {"send_button", "response"}
        )

    def test_missing_fields_mean_no_actions(self) -> None:
        self.assertEqual(revival_module._changed_actions({}), set())
        self.assertEqual(revival_module._required_actions({}), set())

    def test_non_list_fields_mean_no_actions(self) -> None:
        meta = {"changed_actions": "send_button", "required_actions": None}
        self.assertEqual(revival_module._changed_actions(meta), set())
        self.assertEqual(revival_module._required_actions(meta), set())

    def test_unknown_action_names_are_filtered(self) -> None:
        meta = {
            "changed_actions": ["send_button", "not_a_control"],
            "required_actions": ["response", "not_a_control"],
        }
        self.assertEqual(revival_module._changed_actions(meta), {"send_button"})
        self.assertEqual(revival_module._required_actions(meta), {"response"})


if __name__ == "__main__":
    unittest.main()
