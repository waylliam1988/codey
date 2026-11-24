from __future__ import annotations

import unittest
from unittest import mock
from uuid import UUID

from codey.automation.web_clipboard import copy_action_text


class WebClipboardTests(unittest.TestCase):
    def test_copies_normalized_source_and_restores_clipboard(self) -> None:
        evaluate = mock.Mock(side_effect=(
            "user clipboard",
            None,
            '{"tool":"done","args":{"summary":"reply"}}',
            None,
        ))
        click = mock.Mock()
        grant = mock.Mock()
        page = mock.Mock(evaluate=evaluate, context=mock.Mock(grant_permissions=grant))
        action = mock.Mock(click=click)

        result = copy_action_text(page, action, origin="https://example.test/")

        self.assertEqual(result, '{"tool":"done","args":{"summary":"reply"}}')
        click.assert_called_once_with()
        permissions: list[str] = ["clipboard-read", "clipboard-write"]
        grant.assert_called_once_with(
            permissions,
            origin="https://example.test/",
        )
        restore_args: tuple[object, ...] = evaluate.call_args_list[-1].args
        self.assertEqual(restore_args[1], "user clipboard")
        sentinel_args: tuple[object, ...] = evaluate.call_args_list[1].args
        sentinel = sentinel_args[1]
        assert isinstance(sentinel, str)
        self.assertNotIn("codey", sentinel.lower())

    def test_returns_empty_when_copy_action_does_not_replace_sentinel(self) -> None:
        identity = UUID(int=0)
        page = mock.Mock(evaluate=mock.Mock(side_effect=("old", None, f"__CLIPBOARD_CHECK_{identity.hex}__", None)))
        action = mock.Mock()

        with (
            mock.patch("codey.automation.web_clipboard.uuid.uuid4", return_value=identity),
            mock.patch(
                "codey.automation.web_clipboard.monotonic",
                side_effect=(0.0, 0.0, 3.0),
            ),
        ):
            result = copy_action_text(page, action, origin="https://example.test/")

        self.assertEqual(result, "")

    def test_unreadable_clipboard_prevents_a_copy_transaction(self) -> None:
        evaluate = mock.Mock(side_effect=(RuntimeError("could not read"), None, "", None))
        click = mock.Mock()
        page = mock.Mock(evaluate=evaluate)
        action = mock.Mock(click=click)

        self.assertEqual(copy_action_text(page, action, origin="https://example.test/"), "")
        evaluate.assert_called_once_with("navigator.clipboard.readText()")
        click.assert_not_called()

    def test_restores_clipboard_when_sentinel_write_acknowledgement_is_lost(self) -> None:
        evaluate = mock.Mock(side_effect=("original", RuntimeError("write acknowledgement lost"), None))
        click = mock.Mock()
        page = mock.Mock(evaluate=evaluate)
        action = mock.Mock(click=click)

        self.assertEqual(copy_action_text(page, action, origin="https://example.test/"), "")
        evaluate.assert_called_with("(text) => navigator.clipboard.writeText(text)", "original")
        click.assert_not_called()


if __name__ == "__main__":
    unittest.main()
