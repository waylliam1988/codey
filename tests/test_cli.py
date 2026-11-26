from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import codey.app.cli as cli
from codey.app.cli import _safe_print
from codey.app.headless_runner import HeadlessResult, emit_jsonl


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

    def test_default_output_uses_current_stdout(self) -> None:
        stream = io.StringIO()

        with mock.patch("sys.stdout", stream):
            _safe_print("current stream")

        self.assertEqual(stream.getvalue(), "current stream\n")

    def test_jsonl_default_output_uses_current_stdout(self) -> None:
        stream = io.StringIO()

        with mock.patch("sys.stdout", stream):
            emit_jsonl({"type": "task_done"})

        self.assertEqual(stream.getvalue(), '{"type":"task_done"}\n')


class UiCliTests(unittest.TestCase):
    def test_cmd_ui_defaults_to_the_codey_installation_directory(self) -> None:
        args = mock.Mock(port=6060, state_home="")

        with mock.patch("codey.app.desktop.open_desktop") as serve:
            exit_code = cli.cmd_ui(args)

        self.assertEqual(exit_code, 0)
        serve.assert_called_once_with(Path(cli.__file__).resolve().parents[2] / ".codey", port=6060)


class ProviderCliTests(unittest.TestCase):
    def test_cmd_chat_uses_and_cleans_task_context(self) -> None:
        args = mock.Mock(provider="qwen", port=9222, prompt=["hello"], timeout=30)
        provider = mock.Mock()
        provider.send.return_value = "reply"

        with (
            mock.patch("codey.providers.registry.connect_provider", return_value=provider),
            mock.patch("codey.providers.controls.begin_task_context") as begin_context,
            mock.patch("codey.providers.controls.end_task_context") as end_context,
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
            mock.patch("codey.providers.registry.connect_provider", return_value=provider),
            mock.patch("codey.providers.controls.begin_task_context"),
            mock.patch("codey.providers.controls.end_task_context") as end_context,
            mock.patch.object(cli, "_safe_print"),self.assertRaisesRegex(RuntimeError, "CDP disconnected")
        ):
            cli.cmd_chat(args)

        end_context.assert_called_once_with()

    def test_cmd_agent_propagates_headless_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            args = [
                "agent", "--provider", 'deepseek', "--project", td, "--max-turns", "4", "--state-home", td, 'fix',
            ]

            with (
                mock.patch(
                    "codey.app.headless_runner.run_headless",
                    side_effect=RuntimeError("offline"),
                ),
                self.assertRaisesRegex(RuntimeError, "offline"),
            ):
                cli.main(args)

    def test_cmd_agent_plain_mode_prints_human_summary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            args = [
                "agent", "--provider", 'deepseek', "--project", td, "--max-turns", "4", "--state-home", td, 'fix',
            ]
            stdout = io.StringIO()
            stderr = io.StringIO()

            def fake_headless(request, *, emit_jsonl):
                emit_jsonl({
                    "schema_version": 1,
                    "type": "task_start",
                    "run_id": "run-1",
                    "session_id": "session-1",
                    "mode": "agent",
                    "provider": "deepseek",
                })
                emit_jsonl({
                    "schema_version": 1,
                    "type": "task_done",
                    "run_id": "run-1",
                    "session_id": "session-1",
                    "summary": "plain summary",
                    "stop_reason": "done",
                    "mode": "agent",
                })
                return HeadlessResult(0, "run-1", "session-1", "done")

            with (
                mock.patch("codey.app.headless_runner.run_headless", side_effect=fake_headless),
                mock.patch("sys.stdout", stdout),
                mock.patch("sys.stderr", stderr),
            ):
                exit_code = cli.main(args)

            self.assertEqual(exit_code, 0)
            self.assertEqual(stdout.getvalue(), "plain summary\n")
            self.assertIn("[codey] project:", stderr.getvalue())
            self.assertIn("mode=agent", stderr.getvalue())

    def test_cmd_agent_json_emits_machine_readable_events(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            args = [
                "agent", "--provider", 'qwen', "--project", td, "--max-turns", "4", '--json', 'fix', 'tests',
            ]
            stdout = io.StringIO()
            stderr = io.StringIO()

            def fake_headless(request, *, emit_jsonl):
                emit_jsonl({
                    "schema_version": 1,
                    "type": "task_start",
                    "run_id": "run-1",
                    "session_id": "session-1",
                })
                emit_jsonl({
                    "schema_version": 1,
                    "type": "task_done",
                    "run_id": "run-1",
                    "session_id": "session-1",
                    "stop_reason": "done",
                })
                return HeadlessResult(0, "run-1", "session-1", "done")

            with (
                mock.patch("codey.app.headless_runner.run_headless", side_effect=fake_headless) as run_headless,
                mock.patch("sys.stdout", stdout),
                mock.patch("sys.stderr", stderr),
            ):
                exit_code = cli.main(args)

        self.assertEqual(exit_code, 0)
        rows = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertTrue(all(line.startswith("{") for line in stdout.getvalue().splitlines()))
        self.assertEqual(rows[0]["type"], "task_start")
        self.assertEqual(rows[-1]["type"], "task_done")
        self.assertIn("[codey] project:", stderr.getvalue())
        request = run_headless.call_args.args[0]
        self.assertEqual(request.project, Path(td).resolve())
        self.assertEqual(request.task, "fix tests")
        self.assertEqual(request.provider_id, "qwen")
        self.assertEqual(request.intent, "project")

    def test_cmd_agent_plain_mode_still_prints_summary_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            args = [
                "agent", "--provider", 'deepseek', "--project", td, "--max-turns", "4", "--state-home", td, 'fix',
            ]
            stdout = io.StringIO()

            def fake_headless(request, *, emit_jsonl):
                emit_jsonl({
                    "schema_version": 1,
                    "type": "task_done",
                    "run_id": "run-1",
                    "session_id": "session-1",
                    "summary": "plain summary",
                    "stop_reason": "done",
                    "mode": "agent",
                })
                return HeadlessResult(0, "run-1", "session-1", "done")

            with (
                mock.patch("codey.app.headless_runner.run_headless", side_effect=fake_headless),
                mock.patch("sys.stdout", stdout),
            ):
                exit_code = cli.main(args)

            self.assertEqual(exit_code, 0)
            self.assertEqual(stdout.getvalue(), "plain summary\n")

    def test_cmd_agent_json_readonly_maps_to_headless_intent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            args = [
                "agent", "--provider", 'qwen', "--project", td, "--max-turns", "4", '--json', '--readonly', 'explain',
            ]
            stdout = io.StringIO()

            with (
                mock.patch(
                    "codey.app.headless_runner.run_headless",
                    return_value=HeadlessResult(0, "run-1", "session-1", "done"),
                ) as run_headless,
                mock.patch("sys.stdout", stdout),
            ):
                exit_code = cli.main(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(run_headless.call_args.args[0].intent, "planning_readonly")

    def test_cmd_agent_json_auto_maps_to_headless_intent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            args = [
                "agent", "--provider", 'qwen', "--project", td, "--max-turns", "4", '--json', '--auto', 'route', 'this',
            ]
            stdout = io.StringIO()

            with (
                mock.patch(
                    "codey.app.headless_runner.run_headless",
                    return_value=HeadlessResult(0, "run-1", "session-1", "done"),
                ) as run_headless,
                mock.patch("sys.stdout", stdout),
            ):
                exit_code = cli.main(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(run_headless.call_args.args[0].intent, "auto")

    def test_agent_via_main_uses_parser_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch(
                    "codey.app.headless_runner.run_headless",
                    return_value=HeadlessResult(0, "run-1", "session-1", "done"),
                ) as run_headless,
                mock.patch("sys.stdout", stdout),
                mock.patch("sys.stderr", stderr),
            ):
                exit_code = cli.main([
                    "agent",
                    "--project",
                    td,
                    "--provider",
                    "deepseek",
                    "fix it",
                ])
            self.assertEqual(exit_code, 0)
            self.assertEqual(run_headless.call_count, 1)
            request = run_headless.call_args.args[0]
            self.assertEqual(request.project, Path(td).resolve())
            self.assertEqual(request.task, "fix it")







if __name__ == "__main__":
    unittest.main()
