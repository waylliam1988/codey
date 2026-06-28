from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey import server
from codey.changes import ChangeTracker
from codey.provider_diagnostics import ProviderFailure


class GitChangesTests(unittest.TestCase):
    def test_parse_git_status(self) -> None:
        files = server.parse_git_status(" M codey/server.py\n?? new.txt\nR  old.py -> new.py\n")

        self.assertEqual(files[0]["status"], "M")
        self.assertEqual(files[0]["path"], "codey/server.py")
        self.assertEqual(files[1]["status"], "??")
        self.assertEqual(files[1]["path"], "new.txt")
        self.assertEqual(files[2]["status"], "R")
        self.assertEqual(files[2]["path"], "old.py -> new.py")

    def test_displayable_change_path_filters_generated_caches(self) -> None:
        self.assertFalse(server.is_displayable_change_path("__pycache__/"))
        self.assertFalse(server.is_displayable_change_path("pkg/__pycache__/app.cpython-312.pyc"))
        self.assertFalse(server.is_displayable_change_path(".pytest_cache/v/cache/nodeids"))
        self.assertTrue(server.is_displayable_change_path("app.py"))

    def test_collect_changes_uses_empty_snapshot_for_non_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            data = server.collect_changes(td)

            self.assertTrue(data["ok"], data)
            self.assertEqual(data["mode"], "snapshot")
            self.assertEqual(data["changed_count"], 0)

    def test_collect_changes_uses_snapshot_tracker_for_non_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tracker = ChangeTracker(root)
            tracker.capture_before("app.py")
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

            data = server.collect_changes(root, tracker)

            self.assertTrue(data["ok"], data)
            self.assertEqual(data["mode"], "snapshot")
            self.assertEqual(data["changed_count"], 1)
            self.assertEqual(data["files"][0]["status"], "A")

    @unittest.skipIf(shutil.which("git") is None, "git is not installed")
    def test_collect_git_changes_includes_untracked_text_diff(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            (root / "note.txt").write_text("hello\nworld\n", encoding="utf-8")

            data = server.collect_git_changes(root)

            self.assertTrue(data["ok"], data)
            self.assertEqual(data["mode"], "git")
            self.assertEqual(data["changed_count"], 1)
            self.assertEqual(data["files"][0]["path"], "note.txt")
            self.assertEqual(data["files"][0]["status"], "??")
            self.assertEqual(data["files"][0]["additions"], 2)
            self.assertIn("+++ b/note.txt", data["diff"])
            self.assertIn("+hello", data["diff"])

    @unittest.skipIf(shutil.which("git") is None, "git is not installed")
    def test_collect_git_changes_filters_generated_cache_directories(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            (root / "app.py").write_text("old\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, text=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Codey Test",
                    "-c",
                    "user.email=codey-test@example.local",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            (root / "app.py").write_text("new\n", encoding="utf-8")
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"cache")

            data = server.collect_git_changes(root)

            self.assertTrue(data["ok"], data)
            self.assertEqual([file["path"] for file in data["files"]], ["app.py"])

    @unittest.skipIf(shutil.which("git") is None, "git is not installed")
    def test_collect_changes_prefers_git_for_git_repository(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            tracker = ChangeTracker(root)
            tracker.capture_before("tracked.txt")
            (root / "tracked.txt").write_text("snapshot\n", encoding="utf-8")

            data = server.collect_changes(root, tracker)

            self.assertTrue(data["ok"], data)
            self.assertEqual(data["mode"], "git")
            self.assertEqual(data["files"][0]["path"], "tracked.txt")


class ApprovedShellTests(unittest.TestCase):
    def test_execute_approved_shell_runs_in_project(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            data = server.execute_approved_shell(td, ".", 'python -c "print(\'approved\')"')

            self.assertTrue(data["ok"], data)
            self.assertEqual(data["exit_code"], 0)
            self.assertIn("approved", data["output"])

    def test_execute_approved_shell_rejects_escaped_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            data = server.execute_approved_shell(td, "..", 'python -c "print(\'approved\')"')

            self.assertFalse(data["ok"])
            self.assertIn("escapes project root", data["error"])


class SessionThreadingTests(unittest.TestCase):
    def test_state_opens_a_fresh_provider_connection_each_time(self) -> None:
        class FakeProvider:
            name = "DeepSeek Web"
            location = "https://chat.deepseek.com/"

        state = server.State()
        with mock.patch.object(
            server,
            "connect_provider",
            side_effect=[FakeProvider(), FakeProvider()],
        ) as connected:
            first = state.get_provider("qwen")
            second = state.get_provider("qwen")

        self.assertIsNot(first, second)
        self.assertEqual(connected.call_count, 2)
        self.assertEqual(connected.call_args_list, [mock.call("qwen"), mock.call("qwen")])

    def test_run_task_keeps_selected_provider_through_agent_completion(self) -> None:
        state = server.State()
        events = state.subscribe()
        provider = mock.Mock()
        provider.name = "Qwen Studio"
        provider.location = "https://chat.qwen.ai/"

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=provider) as get_provider,
            mock.patch.object(
                server,
                "agent_run",
                return_value=server.RunResult("complete", "done", 3),
            ) as agent_run,
        ):
            server._run_task("session-1", td, "task", 8, False, "qwen")

        get_provider.assert_called_once_with("qwen")
        self.assertIs(agent_run.call_args.args[0], provider)
        provider.close.assert_called_once_with()
        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        task_done = next(event for event in emitted if event["type"] == "task_done")
        self.assertEqual(task_done["provider"], "qwen")
        self.assertEqual(state.provider_id, "qwen")
        self.assertIn(str(Path(td).resolve()), state.change_trackers)
        self.assertIsNotNone(agent_run.call_args.kwargs["change_tracker"])

    def test_run_task_uses_second_model_for_approved_review(self) -> None:
        state = server.State()
        events = state.subscribe()
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        writer.location = "https://chat.deepseek.com/"
        reviewer = mock.Mock()
        reviewer.name = "Xiaomi MiMo Chat"
        reviewer.location = "https://aistudio.xiaomimimo.com/#/c"
        reviewer.send.return_value = '{"verdict":"approved","summary":"Looks good","findings":[]}'
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "app.py", "status": "M", "additions": 1, "deletions": 1}],
            "diff": "diff --git a/app.py b/app.py\n-old\n+new\n",
        }

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=writer),
            mock.patch.object(
                server,
                "agent_run",
                return_value=server.RunResult("complete", "done", 3),
            ) as agent_run,
            mock.patch.object(server, "collect_changes", return_value=changes),
            mock.patch.object(server, "connect_existing_provider", return_value=reviewer) as connect_review,
        ):
            server._run_task("session-1", td, "task", 8, False, "deepseek")

        self.assertEqual(agent_run.call_count, 1)
        connect_review.assert_called_once_with("mimo")
        reviewer.new_chat.assert_called_once_with()
        reviewer.send.assert_called_once()
        reviewer.close.assert_called_once_with()
        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        review_event = next(event for event in emitted if event["type"] == "review")
        self.assertEqual(review_event["text"], "MiMo approved")
        task_done = next(event for event in emitted if event["type"] == "task_done")
        self.assertEqual(task_done["summary"], "complete")

    def test_run_task_sends_review_findings_back_to_writer_once(self) -> None:
        state = server.State()
        events = state.subscribe()
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        writer.location = "https://chat.deepseek.com/"
        reviewer = mock.Mock()
        reviewer.name = "Xiaomi MiMo Chat"
        reviewer.location = "https://aistudio.xiaomimimo.com/#/c"
        reviewer.send.return_value = (
            '{"verdict":"changes_requested","summary":"Fix one issue",'
            '"findings":[{"path":"app.py","issue":"Missing empty case",'
            '"suggested_fix":"Add a guard"}]}'
        )
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "app.py", "status": "M", "additions": 1, "deletions": 1}],
            "diff": "diff --git a/app.py b/app.py\n-old\n+new\n",
        }

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=writer),
            mock.patch.object(
                server,
                "agent_run",
                side_effect=[
                    server.RunResult("first pass", "done", 3),
                    server.RunResult("review fixed", "done", 2),
                ],
            ) as agent_run,
            mock.patch.object(server, "collect_changes", return_value=changes),
            mock.patch.object(server, "connect_existing_provider", return_value=reviewer),
        ):
            server._run_task("session-1", td, "task", 20, False, "deepseek")

        self.assertEqual(agent_run.call_count, 2)
        followup = agent_run.call_args_list[1].args[2]
        self.assertIn("second model reviewed", followup)
        self.assertIn("Missing empty case", followup)
        self.assertFalse(agent_run.call_args_list[1].kwargs["fresh_chat"])
        self.assertLessEqual(agent_run.call_args_list[1].kwargs["max_turns"], server.REVIEW_FIX_TURNS)
        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        review_event = next(event for event in emitted if event["type"] == "review")
        self.assertEqual(review_event["text"], "MiMo suggested changes")
        task_done = next(event for event in emitted if event["type"] == "task_done")
        self.assertEqual(task_done["summary"], "review fixed")

    def test_run_task_falls_back_to_one_model_when_review_unavailable(self) -> None:
        state = server.State()
        events = state.subscribe()
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        writer.location = "https://chat.deepseek.com/"
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "app.py", "status": "M", "additions": 1, "deletions": 1}],
            "diff": "diff --git a/app.py b/app.py\n-old\n+new\n",
        }

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=writer),
            mock.patch.object(
                server,
                "agent_run",
                return_value=server.RunResult("complete", "done", 3),
            ) as agent_run,
            mock.patch.object(server, "collect_changes", return_value=changes),
            mock.patch.object(server, "connect_existing_provider", side_effect=RuntimeError("not open")),
        ):
            server._run_task("session-1", td, "task", 8, False, "deepseek")

        self.assertEqual(agent_run.call_count, 1)
        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        review_event = next(event for event in emitted if event["type"] == "review")
        self.assertEqual(review_event["text"], "Unavailable. Continued with one model.")
        task_done = next(event for event in emitted if event["type"] == "task_done")
        self.assertEqual(task_done["summary"], "complete")

    def test_run_task_repairs_invalid_review_json_once(self) -> None:
        state = server.State()
        events = state.subscribe()
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        writer.location = "https://chat.deepseek.com/"
        reviewer = mock.Mock()
        reviewer.name = "Xiaomi MiMo Chat"
        reviewer.location = "https://aistudio.xiaomimimo.com/#/c"
        reviewer.send.side_effect = [
            "looks good but not json",
            '{"verdict":"approved","summary":"Looks good","findings":[]}',
        ]
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "app.py", "status": "M", "additions": 1, "deletions": 1}],
            "diff": "diff --git a/app.py b/app.py\n-old\n+new\n",
        }

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=writer),
            mock.patch.object(
                server,
                "agent_run",
                return_value=server.RunResult("complete", "done", 3),
            ),
            mock.patch.object(server, "collect_changes", return_value=changes),
            mock.patch.object(server, "connect_existing_provider", return_value=reviewer),
        ):
            server._run_task("session-1", td, "task", 8, False, "deepseek")

        self.assertEqual(reviewer.send.call_count, 2)
        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        review_event = next(event for event in emitted if event["type"] == "review")
        self.assertEqual(review_event["text"], "MiMo approved")

    def test_run_task_skips_review_when_there_are_no_changes(self) -> None:
        state = server.State()
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        writer.location = "https://chat.deepseek.com/"

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=writer),
            mock.patch.object(
                server,
                "agent_run",
                return_value=server.RunResult("complete", "done", 3),
            ),
            mock.patch.object(
                server,
                "collect_changes",
                return_value={"ok": True, "changed_count": 0, "files": [], "diff": ""},
            ),
            mock.patch.object(server, "connect_existing_provider") as connect_review,
        ):
            server._run_task("session-1", td, "task", 8, False, "deepseek")

        connect_review.assert_not_called()

    def test_run_task_emits_provider_failure_diagnostic_on_error(self) -> None:
        state = server.State()
        events = state.subscribe()
        provider = mock.Mock()
        provider.name = "MiMo"
        provider.location = "https://aistudio.xiaomimimo.com/#/c"
        provider.last_failure = ProviderFailure(
            model="MiMo",
            action="send",
            url="https://aistudio.xiaomimimo.com/#/c",
            title="MiMo",
            message="response timed out",
            time="2026-06-28T01:02:03+00:00",
        )

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=provider),
            mock.patch.object(server, "agent_run", side_effect=TimeoutError("response timed out")),
        ):
            server._run_task("session-1", td, "task", 8, False, "mimo")

        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        task_done = next(event for event in emitted if event["type"] == "task_done")
        self.assertEqual(task_done["stop_reason"], "error")
        self.assertEqual(task_done["provider_failure"], {
            "model": "MiMo",
            "action": "send",
            "url": "https://aistudio.xiaomimimo.com/#/c",
            "title": "MiMo",
            "message": "response timed out",
            "time": "2026-06-28T01:02:03+00:00",
        })
        self.assertIs(state.last_provider_failure, provider.last_failure)

    def test_run_task_records_connect_failure_without_provider_page(self) -> None:
        state = server.State()
        events = state.subscribe()

        with (
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", side_effect=RuntimeError("Edge not reachable")),
        ):
            server._run_task("session-1", None, "hello", 8, False, "qwen")

        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        task_done = next(event for event in emitted if event["type"] == "task_done")
        failure = task_done["provider_failure"]
        self.assertEqual(failure["model"], "Qwen")
        self.assertEqual(failure["action"], "connect")
        self.assertEqual(failure["url"], "")
        self.assertEqual(failure["title"], "")
        self.assertEqual(failure["message"], "Edge not reachable")

    def test_restore_snapshot_changes_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("old\n", encoding="utf-8")
            tracker = ChangeTracker(root)
            tracker.capture_before("app.py")
            path.write_text("new\n", encoding="utf-8")
            tracker.collect()

            status, payload = server.restore_snapshot_changes(root, tracker, ["app.py"])

            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"], payload)
            self.assertEqual(payload["restored"], ["app.py"])
            self.assertEqual(path.read_text(encoding="utf-8"), "old\n")

    def test_restore_snapshot_changes_reports_missing_tracker(self) -> None:
        status, payload = server.restore_snapshot_changes("E:/missing", None)

        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])


class UiLaunchTests(unittest.TestCase):
    def test_serve_launches_pywebview_window(self) -> None:
        fake_webview = mock.Mock()

        with (
            mock.patch.dict("sys.modules", {"webview": fake_webview}),
            mock.patch.object(server, "ThreadingHTTPServer") as httpd_cls,
        ):
            httpd = mock.Mock()
            httpd.server_address = ("127.0.0.1", 43210)
            httpd_cls.return_value = httpd

            def stop_server() -> None:
                raise KeyboardInterrupt

            httpd.serve_forever.side_effect = stop_server

            server.serve(port=0)

        fake_webview.create_window.assert_called_once()
        fake_webview.start.assert_called_once()
        httpd.shutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
