from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from codey import profile_doctor, provider_controls
from codey import server
from codey.agent import RunResult
from codey.changes import ChangeTracker
from codey.provider_diagnostics import ProviderFailure
from codey.provider_discovery import Discovery


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


class ProviderStatusTests(unittest.TestCase):
    def test_provider_payload_marks_available_models(self) -> None:
        payload = server.provider_payload({"deepseek": True, "mimo": False})

        by_id = {item["id"]: item for item in payload}
        self.assertTrue(by_id["deepseek"]["available"])
        self.assertFalse(by_id["mimo"]["available"])
        self.assertFalse(by_id["qwen"]["available"])

    def test_provider_status_update_only_reports_changed_model(self) -> None:
        payload = server.provider_status_update("deepseek", True)

        self.assertEqual(payload, [{"id": "deepseek", "label": "DeepSeek", "available": True}])

    def test_provider_availability_reads_cdp_tabs_without_connecting(self) -> None:
        with (
            mock.patch.object(
                server,
                "provider_tab_availability",
                return_value={"deepseek": True, "mimo": True, "qwen": False},
            ) as detected,
            mock.patch.object(server, "connect_existing_provider") as connected,
        ):
            statuses = server.provider_availability()

        self.assertEqual(statuses, {"deepseek": True, "mimo": True, "qwen": False})
        detected.assert_called_once_with()
        connected.assert_not_called()


class SessionThreadingTests(unittest.TestCase):
    def test_conversation_state_is_bounded(self) -> None:
        state = server.State()

        for index in range(server.MAX_CONVERSATION_STATES + 1):
            state.conversation_for(f"session-{index}")

        self.assertEqual(len(state.conversations), server.MAX_CONVERSATION_STATES)
        self.assertNotIn("session-0", state.conversations)

    def test_state_opens_provider_connection_each_time(self) -> None:
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

    def test_state_marks_provider_available_after_connect(self) -> None:
        class FakeProvider:
            name = "Xiaomi MiMo Chat"
            location = "https://aistudio.xiaomimimo.com/#/c"

        state = server.State()
        events = state.subscribe()
        with mock.patch.object(server, "connect_provider", return_value=FakeProvider()):
            state.get_provider("mimo")

        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        providers = next(event for event in emitted if event["type"] == "providers")
        self.assertEqual(providers["providers"], [{"id": "mimo", "label": "MiMo", "available": True}])

    def test_state_pauses_for_control_teaching_and_resumes_with_captured_control(self) -> None:
        state = server.State()
        events = state.subscribe()
        page = mock.Mock()
        page.url = "https://chat.qwen.ai/"
        request = provider_controls.ControlTeachRequest(
            provider_id="qwen",
            action=provider_controls.CONTROL_SEND_BUTTON,
            page=page,
            session_id="session-1",
            require_enabled=True,
        )
        captured = provider_controls.CapturedControl(
            fingerprint={"tag": "button", "role": "button", "text": "Send"},
            current_selector='[data-codey-teach-current="token"]',
        )
        button = mock.Mock()

        with (
            mock.patch.object(provider_controls, "start_click_capture", return_value="token") as start,
            mock.patch.object(provider_controls, "finish_click_capture", return_value=captured) as finish,
            mock.patch.object(provider_controls, "resolve_captured_control", return_value=button) as resolve,
        ):
            slot: list[object] = []
            thread = threading.Thread(target=lambda: slot.append(state.handle_control_teach(request)))
            thread.start()
            emitted = events.get(timeout=1)
            pending_id = emitted["id"]
            with state.lock:
                state.pending_teach[pending_id]["event"].set()
            thread.join(timeout=1)

        self.assertEqual(slot, [button])
        self.assertEqual(emitted["type"], "teach_request")
        self.assertEqual(emitted["text"], "Click the send button in the model page")
        start.assert_called_once_with(page)
        finish.assert_called_once_with(page, "token", provider_controls.CONTROL_SEND_BUTTON, timeout=1.0)
        resolve.assert_called_once_with(request, captured)
        self.assertEqual(state.pending_teach, {})

    def test_profile_doctor_uses_one_healthy_sibling_model_call(self) -> None:
        state = server.State()
        state.set_provider_session("qwen", "old-session")
        page = mock.Mock()
        helper = mock.Mock()
        helper.send.return_value = '{"candidate_id":"c1"}'
        request = profile_doctor.make_request(
            "deepseek",
            provider_controls.CONTROL_SEND_BUTTON,
            page,
            (Discovery(mock.Mock(), {"tag": "button", "ariaLabel": "Send"}, 50),),
        )

        with mock.patch.object(
            server,
            "borrow_open_provider",
            side_effect=[None, helper],
        ) as borrowed:
            selected = state.handle_profile_doctor(request)

        self.assertEqual(selected, "c1")
        self.assertEqual(
            borrowed.call_args_list,
            [mock.call("mimo", page), mock.call("qwen", page)],
        )
        helper.new_chat.assert_called_once_with()
        helper.send.assert_called_once()
        self.assertEqual(helper.send.call_args.kwargs["timeout"], server.PROFILE_DOCTOR_TIMEOUT)
        helper.close.assert_called_once_with()
        self.assertTrue(state.provider_session_changed("qwen", "old-session"))

    def test_profile_doctor_does_not_try_another_model_after_call_failure(self) -> None:
        state = server.State()
        page = mock.Mock()
        first = mock.Mock()
        first.send.side_effect = TimeoutError("failed")
        second = mock.Mock()
        request = profile_doctor.make_request(
            "deepseek",
            provider_controls.CONTROL_SEND_BUTTON,
            page,
            (Discovery(mock.Mock(), {"tag": "button", "ariaLabel": "Send"}, 50),),
        )

        with mock.patch.object(
            server,
            "borrow_open_provider",
            side_effect=[first, second],
        ) as borrowed:
            selected = state.handle_profile_doctor(request)

        self.assertIsNone(selected)
        borrowed.assert_called_once_with("mimo", page)
        first.send.assert_called_once()
        second.send.assert_not_called()

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
                return_value=RunResult("complete", "done", 3, True),
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
        self.assertEqual(task_done["receipt"]["text"], "No files changed · checks passed")
        self.assertEqual(state.provider_id, "qwen")
        self.assertIn(str(Path(td).resolve()), state.change_trackers)
        self.assertIsNotNone(agent_run.call_args.kwargs["change_tracker"])

    def test_run_task_reads_empty_file_without_error_or_legacy_log_event(self) -> None:
        state = server.State()
        events = state.subscribe()
        provider = mock.Mock()
        provider.name = "DeepSeek Web"
        provider.location = "https://chat.deepseek.com/"
        provider.send.side_effect = [
            '{"tool":"read_file","args":{"path":"empty.txt"}}',
            '{"tool":"done","args":{"summary":"empty file read"}}',
        ]

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=provider),
        ):
            Path(td, "empty.txt").write_text("", encoding="utf-8")
            server._run_task("session-empty", td, "Read empty.txt", 8, False, "deepseek")

        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        tool = next(event for event in emitted if event["type"] == "tool")
        done = next(event for event in emitted if event["type"] == "task_done")
        self.assertEqual(tool["result"], "")
        self.assertEqual(done["stop_reason"], "done")
        self.assertFalse(any(event["type"] == "log" for event in emitted))

    def test_plain_chat_followup_reuses_same_model_conversation(self) -> None:
        state = server.State()
        first = mock.Mock()
        first.name = "DeepSeek Web"
        first.location = "https://chat.deepseek.com/"
        first.send.return_value = "First answer"
        second = mock.Mock()
        second.name = "DeepSeek Web"
        second.location = "https://chat.deepseek.com/"
        second.send.return_value = "Second answer"

        with (
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", side_effect=[first, second]),
        ):
            server._run_task("session-1", None, "First question", 8, False, "deepseek")
            server._run_task("session-1", None, "Follow-up question", 8, False, "deepseek")

        first.new_chat.assert_called_once_with()
        second.new_chat.assert_not_called()
        second.send.assert_called_once_with("Follow-up question")

    def test_plain_chat_model_switch_uses_hidden_handoff(self) -> None:
        state = server.State()
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        writer.location = "https://chat.deepseek.com/"
        writer.send.return_value = "The chosen database is SQLite."
        next_model = mock.Mock()
        next_model.name = "Qwen Studio"
        next_model.location = "https://chat.qwen.ai/"
        next_model.send.return_value = "We can continue with SQLite."

        with (
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", side_effect=[writer, next_model]),
        ):
            server._run_task("session-1", None, "Choose a database", 8, False, "deepseek")
            server._run_task("session-1", None, "Add a migration plan", 8, False, "qwen")

        next_model.new_chat.assert_called_once_with()
        prompt = next_model.send.call_args.args[0]
        self.assertIn("Factual handoff", prompt)
        self.assertIn("Choose a database", prompt)
        self.assertIn("The chosen database is SQLite.", prompt)
        self.assertIn("Add a migration plan", prompt)

    def test_returning_to_session_restores_its_model_chat(self) -> None:
        state = server.State()
        first_a = mock.Mock()
        first_a.name = "DeepSeek Web"
        first_a.location = "https://chat.deepseek.com/"
        first_a.send.return_value = "Session A chose SQLite."
        session_b = mock.Mock()
        session_b.name = "DeepSeek Web"
        session_b.location = "https://chat.deepseek.com/"
        session_b.send.return_value = "Session B answer."
        second_a = mock.Mock()
        second_a.name = "DeepSeek Web"
        second_a.location = "https://chat.deepseek.com/"
        second_a.send.return_value = "Session A continued."

        with (
            mock.patch.object(server, "STATE", state),
            mock.patch.object(
                state,
                "get_provider",
                side_effect=[first_a, session_b, second_a],
            ),
        ):
            server._run_task("session-a", None, "Choose a database", 8, False, "deepseek")
            server._run_task("session-b", None, "Explain Python", 8, False, "deepseek")
            server._run_task("session-a", None, "Add migrations", 8, False, "deepseek")

        second_a.new_chat.assert_called_once_with()
        prompt = second_a.send.call_args.args[0]
        self.assertIn("Factual handoff", prompt)
        self.assertIn("Session A chose SQLite.", prompt)
        self.assertNotIn("Session B answer.", prompt)

    def test_project_continue_starts_fresh_chat_with_original_goal(self) -> None:
        state = server.State()
        first = mock.Mock()
        first.name = "DeepSeek Web"
        first.location = "https://chat.deepseek.com/"
        first.send.return_value = '{"tool":"done","args":{"summary":"first pass"}}'
        second = mock.Mock()
        second.name = "DeepSeek Web"
        second.location = "https://chat.deepseek.com/"
        second.send.side_effect = [
            '{"goal":"Build the calculator","current_state":"First pass complete"}',
            '{"tool":"done","args":{"summary":"continued"}}',
        ]

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", side_effect=[first, second]),
        ):
            server._run_task("session-1", td, "Build the calculator", 8, False, "deepseek")
            server._run_task(
                "session-1",
                td,
                "Continue the unfinished task.",
                8,
                True,
                "deepseek",
            )

        first.new_chat.assert_called_once_with()
        second.new_chat.assert_called_once_with()
        self.assertIn("Return only one compact JSON object", second.send.call_args_list[0].args[0])
        prompt = second.send.call_args_list[1].args[0]
        self.assertIn("Factual handoff", prompt)
        self.assertIn("Build the calculator", prompt)
        self.assertIn("Continue the unfinished task.", prompt)

    def test_run_task_emits_receipt_and_inline_changes(self) -> None:
        state = server.State()
        events = state.subscribe()
        provider = mock.Mock()
        provider.name = "DeepSeek Web"
        provider.location = "https://chat.deepseek.com/"
        changes = {
            "ok": True,
            "mode": "snapshot",
            "changed_count": 2,
            "files": [
                {"path": "app.py", "status": "M", "additions": 1, "deletions": 1},
                {"path": "test_app.py", "status": "A", "additions": 5, "deletions": 0},
                {"path": "README.md", "status": "M", "additions": 1, "deletions": 0},
                {"path": "extra.py", "status": "A", "additions": 1, "deletions": 0},
            ],
            "diff": "+new",
        }

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=provider),
            mock.patch.object(
                server,
                "agent_run",
                return_value=RunResult("complete", "done", 3, True),
            ),
            mock.patch.object(server, "collect_changes", return_value=changes),
            mock.patch.object(server, "connect_existing_provider", side_effect=RuntimeError("not open")),
        ):
            server._run_task("session-1", td, "task", 8, False, "deepseek")

        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        task_done = next(event for event in emitted if event["type"] == "task_done")
        self.assertEqual(task_done["receipt"]["text"], "2 files changed · checks passed · restore available")
        self.assertEqual(task_done["changes"]["changed_count"], 2)
        self.assertEqual(len(task_done["changes"]["files"]), 3)
        self.assertEqual(task_done["changes"]["project"], td)

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
                return_value=RunResult("complete", "done", 3),
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
                    RunResult("first pass", "done", 3),
                    RunResult("review fixed", "done", 2),
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
                return_value=RunResult("complete", "done", 3),
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
                return_value=RunResult("complete", "done", 3),
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
                return_value=RunResult("complete", "done", 3),
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

    def test_run_task_treats_control_teaching_cancel_as_stop(self) -> None:
        state = server.State()
        events = state.subscribe()
        provider = mock.Mock()
        provider.name = "Qwen Studio"
        provider.location = "https://chat.qwen.ai/"

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=provider),
            mock.patch.object(
                server,
                "agent_run",
                side_effect=provider_controls.ControlTeachCancelled("cancelled"),
            ),
        ):
            server._run_task("session-1", td, "task", 8, False, "qwen")

        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        task_done = next(event for event in emitted if event["type"] == "task_done")
        self.assertEqual(task_done["stop_reason"], "stopped")
        self.assertEqual(task_done["summary"], "")
        self.assertIsNone(task_done["provider_failure"])

    def test_restore_snapshot_changes_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("old\n", encoding="utf-8")
            tracker = ChangeTracker(root)
            tracker.capture_before("app.py")
            path.write_text("new\n", encoding="utf-8")
            tracker.capture_after("app.py")
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

    @unittest.skipIf(shutil.which("git") is None, "git is not installed")
    def test_git_task_does_not_persist_recovery_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            path = root / "app.py"
            path.write_text("old\n", encoding="utf-8")
            state = server.State(state_td)
            provider = mock.Mock()
            provider.name = "DeepSeek Web"

            def fake_agent_run(*_args, **kwargs):
                tracker = kwargs["change_tracker"]
                tracker.capture_before("app.py")
                path.write_text("new\n", encoding="utf-8")
                tracker.capture_after("app.py")
                return RunResult("complete", "done", 1, True)

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(server, "agent_run", side_effect=fake_agent_run),
                mock.patch.object(
                    server,
                    "collect_changes",
                    return_value={"ok": True, "changed_count": 0, "files": [], "diff": ""},
                ),
            ):
                server._run_task("session-1", str(root), "task", 4, False, "deepseek")

            self.assertFalse(state.snapshot_store.path_for(root).exists())

    @unittest.skipIf(shutil.which("git") is None, "git is not installed")
    def test_cached_tracker_switches_to_git_mode_after_repository_init(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("old\n", encoding="utf-8")
            state = server.State(state_td)
            snapshot_tracker = state.change_tracker_for(root, persistent=True)
            snapshot_tracker.capture_before("app.py")
            path.write_text("new\n", encoding="utf-8")
            snapshot_tracker.capture_after("app.py")
            recovery = state.snapshot_store.path_for(root)
            self.assertTrue(recovery.is_file())

            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
            git_tracker = state.change_tracker_for(root, persistent=False)
            git_tracker.capture_before("app.py")
            path.write_text("newer\n", encoding="utf-8")
            git_tracker.capture_after("app.py")
            snapshot_tracker.capture_after("app.py")

            self.assertIsNot(snapshot_tracker, git_tracker)
            self.assertIsNone(git_tracker.store)
            self.assertIsNone(snapshot_tracker.store)
            self.assertFalse(recovery.exists())


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
