from __future__ import annotations

import unittest
from unittest import mock

from codey.web_clipboard import copy_action_text


class WebClipboardTests(unittest.TestCase):
    def test_copies_normalized_source_and_restores_clipboard(self) -> None:
        page = mock.Mock()
        action = mock.Mock()
        page.evaluate.side_effect = [
            "user clipboard",
            None,
            '{"tool":"done","args":{"summary":"reply"}}',
            None,
        ]

        result = copy_action_text(page, action, origin="https://example.test/")

        self.assertEqual(result, '{"tool":"done","args":{"summary":"reply"}}')
        action.click.assert_called_once_with()
        page.context.grant_permissions.assert_called_once_with(
            ["clipboard-read", "clipboard-write"],
            origin="https://example.test/",
        )
        self.assertEqual(page.evaluate.call_args_list[-1].args[1], "user clipboard")

    def test_returns_empty_when_copy_action_does_not_replace_sentinel(self) -> None:
        page = mock.Mock()
        action = mock.Mock()
        page.evaluate.side_effect = ["old", None, "__CODEY_CLIPBOARD_test__", None]

        with (
            mock.patch("codey.web_clipboard.uuid.uuid4") as uuid4,
            mock.patch("codey.web_clipboard.time.time", side_effect=[0.0, 0.0, 3.0]),
        ):
            uuid4.return_value.hex = "test"
            result = copy_action_text(page, action, origin="https://example.test/")

        self.assertEqual(result, "")


if __name__ == "__main__":
    unittest.main()
