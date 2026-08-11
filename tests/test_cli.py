from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey.agent import RunResult
from codey.cli import _safe_print
from codey.headless_runner import HeadlessResult
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
        args = mock.Mock(provider="stepfun", port=9222, project=".", task=["fix"], max_turns=4)

        with (
            mock.patch("codey.providers.connect_provider", side_effect=RuntimeError("offline")),
            mock.patch("codey.provider_controls.begin_task_context") as begin_context,
            mock.patch("codey.provider_controls.end_task_context") as end_context,
            mock.patch.object(cli, "_safe_print"),
        ):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                cli.cmd_agent(args)

        begin_context.assert_called_once_with("cli-agent:stepfun")
        end_context.assert_called_once_with()

    def test_cmd_agent_json_emits_machine_readable_events(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            args = mock.Mock(
                provider="qwen",
                port=9222,
                project=td,
                task=["fix", "tests"],
                max_turns=4,
                json=True,
                readonly=False,
                state_home="",
            )
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
                mock.patch("codey.headless_runner.run_headless", side_effect=fake_headless) as run_headless,
                mock.patch("sys.stdout", stdout),
                mock.patch("sys.stderr", stderr),
            ):
                exit_code = cli.cmd_agent(args)

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

    def test_cmd_agent_json_readonly_maps_to_headless_intent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            args = mock.Mock(
                provider="qwen",
                port=9222,
                project=td,
                task=["explain"],
                max_turns=4,
                json=True,
                readonly=True,
                state_home="",
            )
            stdout = io.StringIO()

            with (
                mock.patch(
                    "codey.headless_runner.run_headless",
                    return_value=HeadlessResult(0, "run-1", "session-1", "done"),
                ) as run_headless,
                mock.patch("sys.stdout", stdout),
            ):
                exit_code = cli.cmd_agent(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(run_headless.call_args.args[0].intent, "planning_readonly")

    def test_cmd_agent_json_auto_maps_to_headless_intent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            args = mock.Mock(
                provider="qwen",
                port=9222,
                project=td,
                task=["route", "this"],
                max_turns=4,
                json=True,
                readonly=False,
                auto=True,
                state_home="",
            )
            stdout = io.StringIO()

            with (
                mock.patch(
                    "codey.headless_runner.run_headless",
                    return_value=HeadlessResult(0, "run-1", "session-1", "done"),
                ) as run_headless,
                mock.patch("sys.stdout", stdout),
            ):
                exit_code = cli.cmd_agent(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(run_headless.call_args.args[0].intent, "auto")

    def test_cmd_ghost_directive_exports_bounded_preview(self) -> None:
        from codey.ghost.hebbian import GhostHebbianStore
        from codey.ghost.inbox import GhostInboxStore
        from codey.ghost.schema import GhostSignal, GhostSignalParseResult

        with tempfile.TemporaryDirectory() as td:
            inbox = GhostInboxStore(td)
            created = inbox.ingest_signals(
                GhostSignalParseResult(
                    signals=(
                        GhostSignal(
                            kind="style_preference",
                            scope="user",
                            summary="Prefer concise answer-first replies.",
                            evidence_quote="以后先给结论",
                            confidence=0.9,
                            metadata={
                                "conflict_key": "reply_structure",
                                "value_key": "answer_first",
                            },
                            source="test",
                        ),
                    ),
                    ok=True,
                    provider_id="test",
                ),
                session_id="s1",
                run_id="r1",
                user_text="以后先给结论",
            )
            assert len(created) == 1
            GhostHebbianStore(td).reinforce_candidate(created[0])
            args = mock.Mock(
                ghost_cmd="directive",
                state_home=td,
                project="",
                session_id="s1",
                budget=900,
            )
            stdout = io.StringIO()

            with mock.patch("sys.stdout", stdout):
                exit_code = cli.cmd_ghost(args)

            args.budget = 0
            zero_budget_stdout = io.StringIO()
            with mock.patch("sys.stdout", zero_budget_stdout):
                zero_budget_exit_code = cli.cmd_ghost(args)

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["ok"])
        self.assertIn("Local Context:", payload["text"])
        self.assertNotIn("Ghost", payload["text"])
        self.assertIn("reply structure = answer first", payload["text"])
        self.assertNotIn("Prefer concise answer-first replies.", payload["text"])
        self.assertEqual(payload["selected_count"], 1)
        self.assertEqual(zero_budget_exit_code, 0)
        zero_budget_payload = json.loads(zero_budget_stdout.getvalue())
        self.assertTrue(zero_budget_payload["ok"])
        self.assertEqual(zero_budget_payload["text"], "")
        self.assertEqual(zero_budget_payload["selected_count"], 0)
        self.assertTrue(zero_budget_payload["truncated"])

    def test_cmd_ghost_continuity_exports_bounded_preview(self) -> None:
        from codey.ghost.continuity import GhostContinuityStore

        with tempfile.TemporaryDirectory() as td:
            store = GhostContinuityStore(td)
            store.sync_from_sources(
                user_focus_excerpt="Continue the bounded continuity projection",
                session_id="s1",
                run_id="r1",
                mode="chat",
            )
            args = mock.Mock(
                ghost_cmd="continuity",
                state_home=td,
                project="",
                session_id="s1",
                budget=900,
            )
            stdout = io.StringIO()

            with mock.patch("sys.stdout", stdout):
                exit_code = cli.cmd_ghost(args)

            args.budget = 0
            zero_budget_stdout = io.StringIO()
            with mock.patch("sys.stdout", zero_budget_stdout):
                zero_budget_exit_code = cli.cmd_ghost(args)

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["ok"])
        self.assertIn("Local Context:", payload["text"])
        self.assertIn("Bounded local continuity", payload["text"])
        self.assertIn("bounded continuity projection", payload["text"])
        self.assertNotIn("Ghost", payload["text"])
        self.assertEqual(payload["selected_count"], 1)
        self.assertEqual(zero_budget_exit_code, 0)
        zero_budget_payload = json.loads(zero_budget_stdout.getvalue())
        self.assertTrue(zero_budget_payload["ok"])
        self.assertEqual(zero_budget_payload["text"], "")
        self.assertEqual(zero_budget_payload["selected_count"], 0)
        self.assertTrue(zero_budget_payload["truncated"])

    def test_cmd_ghost_export_includes_router_and_sleep_state(self) -> None:
        from codey.ghost.router import GhostRouteDecision, GhostRouteRequest, GhostRouteStore, finalize_route_decision
        from codey.ghost.sleep import GhostSleepStore

        with tempfile.TemporaryDirectory() as td:
            router = GhostRouteStore(td)
            request = GhostRouteRequest(
                task="do not store this full task",
                baseline_mode="chat",
                session_id="s1",
                run_id="r1",
                provider_id="deepseek",
            )
            router.append_result(
                finalize_route_decision(
                    request,
                    GhostRouteDecision("research", 0.9, "fresh", True),
                ),
                request,
            )
            GhostSleepStore(td).run_once(run_id="r1", session_id="s1")
            args = mock.Mock(ghost_cmd="export", state_home=td)
            stdout = io.StringIO()

            with mock.patch("sys.stdout", stdout):
                exit_code = cli.cmd_ghost(args)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertIn("router", payload)
        self.assertIn("router_events", payload["router"])
        self.assertNotIn("do not store this full task", json.dumps(payload, ensure_ascii=False))
        self.assertIn("sleep", payload)
        self.assertIn("sleep_events", payload["sleep"])

    def test_cmd_ghost_reset_deletes_router_and_sleep_files(self) -> None:
        from codey.ghost.router import GhostRouteDecision, GhostRouteRequest, GhostRouteStore, finalize_route_decision
        from codey.ghost.sleep import GhostSleepStore

        with tempfile.TemporaryDirectory() as td:
            router = GhostRouteStore(td)
            request = GhostRouteRequest(task="route", baseline_mode="chat", session_id="s1")
            router.append_result(
                finalize_route_decision(
                    request,
                    GhostRouteDecision("research", 0.9, "fresh", True),
                ),
                request,
            )
            sleep = GhostSleepStore(td)
            sleep.run_once(run_id="r1", session_id="s1")
            self.assertTrue(router.state_path.exists())
            self.assertTrue(router.events_path.exists())
            self.assertTrue(sleep.state_path.exists())
            self.assertTrue(sleep.events_path.exists())
            args = mock.Mock(ghost_cmd="reset", state_home=td, yes=True)
            stdout = io.StringIO()

            with mock.patch("sys.stdout", stdout):
                exit_code = cli.cmd_ghost(args)

            self.assertFalse(router.state_path.exists())
            self.assertFalse(router.events_path.exists())
            self.assertFalse(sleep.state_path.exists())
            self.assertFalse(sleep.events_path.exists())

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["router_ok"])
        self.assertTrue(payload["sleep_ok"])

    def test_cmd_ghost_delete_scope_cleans_router_and_sleep_session_refs(self) -> None:
        from codey.ghost.router import GhostRouteDecision, GhostRouteRequest, GhostRouteStore, finalize_route_decision
        from codey.ghost.sleep import GhostSleepStore

        with tempfile.TemporaryDirectory() as td:
            router = GhostRouteStore(td)
            request = GhostRouteRequest(task="route", baseline_mode="chat", session_id="session-delete")
            router.append_result(
                finalize_route_decision(
                    request,
                    GhostRouteDecision("research", 0.9, "fresh", True),
                ),
                request,
            )
            sleep = GhostSleepStore(td)
            sleep.run_once(run_id="r1", session_id="session-delete")
            args = mock.Mock(
                ghost_cmd="delete-scope",
                state_home=td,
                scope_name="session",
                project="",
                session_id="session-delete",
                yes=True,
            )
            stdout = io.StringIO()

            with mock.patch("sys.stdout", stdout):
                exit_code = cli.cmd_ghost(args)

            exported = sleep.export_state()
            router_exported = router.export_state()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["router_removed_count"], 1)
        self.assertEqual(payload["sleep_removed"]["reports"], 1)
        self.assertEqual(exported["sleep"], {})
        self.assertEqual(router_exported["router"]["records"], [])


if __name__ == "__main__":
    unittest.main()
