from __future__ import annotations

import unittest
from unittest import mock

from codey.cli import _safe_print
import codey.cli as cli


class AsciiStream:
    encoding = "ascii"

    def __init__(self) -> None:
        self.text = ""

    def write(self, value: str) -> None:
        value.encode(self.encoding)
        self.text += value


class SafePrintTests(unittest.TestCase):
    def test_replaces_characters_unsupported_by_terminal_encoding(self) -> None:
        stream = AsciiStream()

        _safe_print("Qwen\u00a0reply", file=stream)

        self.assertEqual(stream.text, "Qwen?reply\n")


class UiCliTests(unittest.TestCase):
    def test_cmd_ui_uses_server_serve_without_browser_flag(self) -> None:
        args = mock.Mock(port=6060)

        with mock.patch("codey.server.serve") as serve:
            exit_code = cli.cmd_ui(args)

        self.assertEqual(exit_code, 0)
        serve.assert_called_once_with(host="127.0.0.1", port=6060)


if __name__ == "__main__":
    unittest.main()
