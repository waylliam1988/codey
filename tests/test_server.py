from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey import server


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

    def test_collect_git_changes_rejects_non_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            data = server.collect_git_changes(td)

            self.assertFalse(data["ok"])
            self.assertEqual(data["error"], "not a git repository")

    @unittest.skipIf(shutil.which("git") is None, "git is not installed")
    def test_collect_git_changes_includes_untracked_text_diff(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            (root / "note.txt").write_text("hello\nworld\n", encoding="utf-8")

            data = server.collect_git_changes(root)

            self.assertTrue(data["ok"], data)
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
