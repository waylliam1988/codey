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


class ProviderCliTests(unittest.TestCase):
    def test_cmd_chat_uses_and_cleans_task_context(self) -> None:
        args = mock.Mock(provider="qwen", port=9222, prompt=["hello"], timeout=30)
        provider = mock.Mock()
        provider.send.return_value = "reply"

        with (
            mock.patch("codey.providers.connect_provider", return_value=provider),
            mock.patch("codey.provider_controls.begin_task_context") as begin_context,
            mock.patch("codey.provider_controls.end_task_context") as end_context,
            mock.patch.object(cli, "_safe_print"),
        ):
            exit_code = cli.cmd_chat(args)

        self.assertEqual(exit_code, 0)
        begin_context.assert_called_once_with("cli-chat:qwen")
        end_context.assert_called_once_with()
        provider.close.assert_called_once_with()

    def test_cmd_chat_cleans_task_context_when_provider_close_fails(self) -> None:
        args = mock.Mock(provider="qwen", port=9222, prompt=["hello"], timeout=30)
        provider = mock.Mock()
        provider.send.return_value = "reply"
        provider.close.side_effect = RuntimeError("CDP disconnected")

        with (
            mock.patch("codey.providers.connect_provider", return_value=provider),
            mock.patch("codey.provider_controls.begin_task_context"),
            mock.patch("codey.provider_controls.end_task_context") as end_context,
            mock.patch.object(cli, "_safe_print"),
        ):
            with self.assertRaisesRegex(RuntimeError, "CDP disconnected"):
                cli.cmd_chat(args)

        end_context.assert_called_once_with()

    def test_cmd_agent_cleans_task_context_when_provider_close_fails(self) -> None:
        args = mock.Mock(provider="deepseek", port=9222, project=".", task=["fix"], max_turns=4)
        provider = mock.Mock()
        provider.close.side_effect = RuntimeError("CDP disconnected")

        with (
            mock.patch("codey.providers.connect_provider", return_value=provider),
            mock.patch("codey.agent.run", return_value=mock.Mock(summary="done")),
            mock.patch("codey.provider_controls.begin_task_context"),
            mock.patch("codey.provider_controls.end_task_context") as end_context,
            mock.patch.object(cli, "_safe_print"),
        ):
            with self.assertRaisesRegex(RuntimeError, "CDP disconnected"):
                cli.cmd_agent(args)

        end_context.assert_called_once_with()

    def test_cmd_agent_cleans_task_context_when_provider_connection_fails(self) -> None:
        args = mock.Mock(provider="mimo", port=9222, project=".", task=["fix"], max_turns=4)

        with (
            mock.patch("codey.providers.connect_provider", side_effect=RuntimeError("offline")),
            mock.patch("codey.provider_controls.begin_task_context") as begin_context,
            mock.patch("codey.provider_controls.end_task_context") as end_context,
            mock.patch.object(cli, "_safe_print"),
        ):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                cli.cmd_agent(args)

        begin_context.assert_called_once_with("cli-agent:mimo")
        end_context.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
