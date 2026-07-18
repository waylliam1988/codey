from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.agent import RunResult
from codey.cli import _safe_print
from codey.events import MAX_EVENT_TEXT_CHARS, RunEvent
from codey.models import ToolCall
from codey.tool_runtime import ToolOutcome
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

    def test_cmd_agent_json_emits_machine_readable_events(self) -> None:
        provider = mock.Mock()
        long_output = "x" * 260 + "\nsecond line"

        def fake_run(*_args, **kwargs):
            on_event = kwargs["on_event"]
            call = ToolCall("run", {"path": ".", "command": "python -m pytest -q"})
            on_event(RunEvent.status("[agent] opening a fresh Fake conversation"))
            on_event(RunEvent.turn_started(1, '{"tool":"run","args":{...}}'))
            on_event(RunEvent.tool_started(1, call, "Running python -m pytest -q"))
            on_event(RunEvent.tool_finished(
                1,
                call,
                ToolOutcome(long_output, True, exit_code=0, truncated=True),
            ))
            return RunResult(
                "finished",
                "done",
                1,
                checks_passed=True,
                changed=True,
                checks_ran=True,
            )

        with tempfile.TemporaryDirectory() as td:
            args = mock.Mock(
                provider="qwen",
                port=9222,
                project=td,
                task=["fix", "tests"],
                max_turns=4,
                json=True,
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                mock.patch("codey.providers.connect_provider", return_value=provider),
                mock.patch("codey.agent.run", side_effect=fake_run),
                mock.patch("codey.provider_controls.begin_task_context"),
                mock.patch("codey.provider_controls.end_task_context"),
                mock.patch("codey.cli.uuid.uuid4", return_value=mock.Mock(hex="abc123def456")),
                mock.patch("sys.stdout", stdout),
                mock.patch("sys.stderr", stderr),
            ):
                exit_code = cli.cmd_agent(args)

        self.assertEqual(exit_code, 0)
        rows = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertTrue(all(line.startswith("{") for line in stdout.getvalue().splitlines()))
        self.assertEqual(rows[0], {
            "type": "session",
            "schema_version": 1,
            "run_id": "cli_abc123def456",
            "project": Path(td).resolve().as_posix(),
            "provider": "qwen",
        })
        self.assertEqual(rows[1], {"type": "agent_start", "run_id": "cli_abc123def456"})
        self.assertEqual(rows[2]["type"], "info")
        self.assertIn("opening a fresh", rows[2]["text"])
        self.assertEqual(rows[3]["type"], "turn")
        self.assertEqual(rows[4]["type"], "tool_started")
        self.assertEqual(rows[4]["command"], "python -m pytest -q")
        self.assertEqual(rows[5]["type"], "tool")
        self.assertTrue(rows[5]["ok"])
        self.assertEqual(rows[5]["exit_code"], 0)
        self.assertTrue(rows[5]["truncated"])
        self.assertEqual(rows[5]["result"], "x" * 200)
        self.assertEqual(rows[-1]["type"], "agent_end")
        self.assertEqual(rows[-1]["summary"], "finished")
        self.assertTrue(rows[-1]["checks_passed"])
        self.assertTrue(rows[-1]["checks_ran"])
        self.assertNotEqual(stdout.getvalue().strip(), "finished")
        self.assertIn("[codey] project:", stderr.getvalue())

    def test_cmd_agent_plain_mode_still_prints_summary_text(self) -> None:
        args = mock.Mock(
            provider="deepseek",
            port=9222,
            project=".",
            task=["fix"],
            max_turns=4,
            json=False,
        )
        provider = mock.Mock()
        stdout = io.StringIO()

        with (
            mock.patch("codey.providers.connect_provider", return_value=provider),
            mock.patch("codey.agent.run", return_value=RunResult("plain summary")),
            mock.patch("codey.provider_controls.begin_task_context"),
            mock.patch("codey.provider_controls.end_task_context"),
            mock.patch("sys.stdout", stdout),
        ):
            exit_code = cli.cmd_agent(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), "plain summary\n")

    def test_cmd_agent_json_clips_large_text_fields(self) -> None:
        provider = mock.Mock()
        long_status = "[agent] " + ("status " * 250)
        long_command = "python -m pytest " + ("x" * 2_000)
        long_summary = "summary " * 250

        def fake_run(*_args, **kwargs):
            on_event = kwargs["on_event"]
            call = ToolCall("run", {"path": ".", "command": long_command})
            on_event(RunEvent.status(long_status))
            on_event(RunEvent.tool_started(1, call, "Running long command"))
            on_event(RunEvent.tool_finished(1, call, ToolOutcome("ok", True, exit_code=0)))
            return RunResult(long_summary, "done", 1, checks_ran=True)

        with tempfile.TemporaryDirectory() as td:
            args = mock.Mock(
                provider="qwen",
                port=9222,
                project=td,
                task=["fix"],
                max_turns=4,
                json=True,
            )
            stdout = io.StringIO()

            with (
                mock.patch("codey.providers.connect_provider", return_value=provider),
                mock.patch("codey.agent.run", side_effect=fake_run),
                mock.patch("codey.provider_controls.begin_task_context"),
                mock.patch("codey.provider_controls.end_task_context"),
                mock.patch("sys.stdout", stdout),
            ):
                exit_code = cli.cmd_agent(args)

        self.assertEqual(exit_code, 0)
        rows = [json.loads(line) for line in stdout.getvalue().splitlines()]
        info = next(row for row in rows if row["type"] == "info")
        started = next(row for row in rows if row["type"] == "tool_started")
        tool = next(row for row in rows if row["type"] == "tool")
        end = rows[-1]
        for value in (info["text"], started["command"], tool["command"], end["summary"]):
            self.assertLessEqual(len(value), MAX_EVENT_TEXT_CHARS)
            self.assertTrue(value.endswith("..."))


if __name__ == "__main__":
    unittest.main()
