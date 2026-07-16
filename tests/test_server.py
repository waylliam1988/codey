from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from codey import (
    cancellation,
    changes,
    profile_doctor,
    provider_controls,
    provider_flow,
)
from codey import server
from codey.agent import RunResult
from codey.changes import ChangeTracker
from codey.consensus import ConsensusAdvice, ConsensusResult
from codey.handoff import ConversationSnapshot
from codey.provider_diagnostics import ProviderActionError, ProviderFailure
from codey.provider_discovery import Discovery
from codey.task_runner import _project_has_user_files


class GitChangesTests(unittest.TestCase):
    def test_parse_git_status(self) -> None:
        files = changes.parse_git_status(" M codey/server.py\n?? new.txt\nR  old.py -> new.py\n")

        self.assertEqual(files[0]["status"], "M")
        self.assertEqual(files[0]["path"], "codey/server.py")
        self.assertEqual(files[1]["status"], "??")
        self.assertEqual(files[1]["path"], "new.txt")
        self.assertEqual(files[2]["status"], "R")
        self.assertEqual(files[2]["path"], "old.py -> new.py")

    def test_displayable_change_path_filters_generated_caches(self) -> None:
        self.assertFalse(changes.is_displayable_change_path("__pycache__/"))
        self.assertFalse(changes.is_displayable_change_path("pkg/__pycache__/app.cpython-312.pyc"))
        self.assertFalse(changes.is_displayable_change_path(".pytest_cache/v/cache/nodeids"))
        self.assertTrue(changes.is_displayable_change_path("app.py"))

    def test_collect_changes_uses_empty_snapshot_for_non_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            data = changes.collect_changes(td)

            self.assertTrue(data["ok"], data)
            self.assertEqual(data["mode"], "snapshot")
            self.assertEqual(data["changed_count"], 0)

    def test_collect_changes_uses_snapshot_tracker_for_non_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tracker = ChangeTracker(root)
            tracker.capture_before("app.py")
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

            data = changes.collect_changes(root, tracker)

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

            data = changes.collect_git_changes(root)

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

            data = changes.collect_git_changes(root)

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

            data = changes.collect_changes(root, tracker)

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

    def test_execute_approved_shell_preserves_large_output_head_and_tail(self) -> None:
        completed = subprocess.CompletedProcess(
            "command",
            1,
            stdout="HEAD" + ("x" * 200) + "TAIL",
            stderr="",
        )
        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "SHELL_OUTPUT_LIMIT", 80),
            mock.patch.object(server.subprocess, "run", return_value=completed),
        ):
            data = server.execute_approved_shell(td, ".", "command")

        self.assertTrue(data["truncated"])
        self.assertTrue(data["output"].startswith("HEAD"))
        self.assertTrue(data["output"].endswith("TAIL"))
        self.assertIn("middle of output omitted", data["output"])

    def test_shell_approval_continuation_includes_post_approval_checklist(self) -> None:
        prompt = server.build_shell_approval_continuation(
            command="npm install",
            result={"exit_code": 0, "output": "added 10 packages", "truncated": False},
            post_approval_instructions=(
                "Post-approval checklist:\n"
                "- Inspect the shell exit code and output before claiming success.\n"
                "- If a trusted local check is available, run it before done."
            ),
            setup_context=(
                "Setup Context (read-only diagnosis; no setup commands were run):\n"
                "Local tools:\n"
                "- npm: available"
            ),
        )

        self.assertIn("The user approved and ran this shell command:", prompt)
        self.assertIn("npm install", prompt)
        self.assertIn("Exit code: 0", prompt)
        self.assertIn("Setup Context", prompt)
        self.assertIn("- npm: available", prompt)
        self.assertIn("Post-approval checklist", prompt)
        self.assertIn("trusted local check", prompt)

    def test_shell_continuation_setup_context_is_limited_to_setup_risks(self) -> None:
        with mock.patch.object(server, "safe_setup_context", return_value="Setup Context"):
            setup = server._shell_continuation_setup_context({
                "project": "E:/demo",
                "risk_label": "dependency_install",
            })
            generic = server._shell_continuation_setup_context({
                "project": "E:/demo",
                "risk_label": "generic",
            })

        self.assertEqual(setup, "Setup Context")
        self.assertEqual(generic, "")


class ProviderStatusTests(unittest.TestCase):
    def test_provider_payload_marks_available_models(self) -> None:
        payload = server.provider_payload({"deepseek": True, "mimo": False})

        by_id = {item["id"]: item for item in payload}
        self.assertTrue(by_id["deepseek"]["available"])
        self.assertFalse(by_id["mimo"]["available"])
        self.assertFalse(by_id["qwen"]["available"])
        self.assertFalse(by_id["glm"]["available"])

    def test_provider_status_update_only_reports_changed_model(self) -> None:
        payload = server.provider_status_update("deepseek", True)

        self.assertEqual(payload, [{"id": "deepseek", "label": "DeepSeek", "available": True}])

    def test_provider_availability_reads_cdp_tabs_without_connecting(self) -> None:
        with (
            mock.patch.object(
                server,
                "provider_tab_availability",
                return_value={"deepseek": True, "mimo": True, "qwen": False, "glm": False},
            ) as detected,
            mock.patch.object(server, "connect_existing_provider") as connected,
        ):
            statuses = server.provider_availability()

        self.assertEqual(
            statuses,
            {"deepseek": True, "mimo": True, "qwen": False, "glm": False},
        )
        detected.assert_called_once_with()
        connected.assert_not_called()

    def test_health_filter_excludes_open_provider_from_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.State(td)
            state.provider_supervisor.record_failure(
                "qwen",
                ProviderFailure(
                    "Qwen",
                    "send",
                    "",
                    "",
                    "limited",
                    "now",
                    "rate_limited",
                ),
            )
            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(
                    server,
                    "provider_tab_availability",
                    return_value={
                        "deepseek": True,
                        "mimo": True,
                        "qwen": True,
                        "glm": True,
                    },
                ),
            ):
                statuses = server.provider_availability()
                reviewers = server.reviewer_candidates("deepseek")

        self.assertFalse(statuses["qwen"])
        self.assertNotIn("qwen", reviewers)
        self.assertIn("mimo", reviewers)

    def test_failover_order_prefers_open_tabs_then_registry_order(self) -> None:
        state = server.State()
        with mock.patch.object(
            server,
            "provider_tab_availability",
            return_value={"qwen": True, "glm": True},
        ):
            order = state.provider_failover_order()

        self.assertEqual(order, ("qwen", "glm", "deepseek", "mimo"))

    def test_review_honors_task_cancellation_before_connecting(self) -> None:
        event = threading.Event()
        event.set()
        with (
            cancellation.scope(event),
            mock.patch.object(server, "connect_existing_provider") as connected,
        ):
            with self.assertRaises(cancellation.TaskCancelled):
                server._run_review(
                    session_id="session-1",
                    project="E:/demo",
                    task="task",
                    writer_summary="done",
                    changes={"ok": True, "changed_count": 1, "diff": "+x"},
                    recent_log="",
                    writer_id="deepseek",
                )

        connected.assert_not_called()


class ConsensusConnectionTests(unittest.TestCase):
    def test_consensus_borrows_sibling_tab_from_selected_provider(self) -> None:
        state = server.State()
        selected = mock.Mock()
        selected.session.page = object()
        selected.send.return_value = "combined answer"
        advisor = mock.Mock()
        advisor.name = "MiMo"
        advisor.send.return_value = "advisor note"

        with (
            mock.patch.object(server, "STATE", state),
            mock.patch.object(
                server,
                "provider_availability",
                return_value={"deepseek": True, "mimo": True, "qwen": False, "glm": False},
            ),
            mock.patch.object(server, "borrow_open_provider", return_value=advisor) as borrowed,
            mock.patch.object(
                server,
                "connect_existing_provider",
                side_effect=AssertionError("should borrow sibling tab"),
            ),
        ):
            result = server._run_consensus(
                selected_provider=selected,
                selected_provider_id="deepseek",
                task="Explain breathing apps",
            )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.answer, "combined answer")
        borrowed.assert_called_once_with("mimo", selected.session.page)
        advisor.new_chat.assert_called_once_with()
        advisor.close.assert_called_once_with()
        selected.send.assert_called_once()

    def test_project_audit_borrows_sibling_tab_from_selected_provider(self) -> None:
        state = server.State()
        selected = mock.Mock()
        selected.session.page = object()
        advisor = mock.Mock()
        advisor.name = "MiMo"
        advisor.send.return_value = '{"tool":"done","args":{"summary":"audit report"}}'

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(
                server,
                "provider_availability",
                return_value={"deepseek": True, "mimo": True, "qwen": False, "glm": False},
            ),
            mock.patch.object(server, "borrow_open_provider", return_value=advisor) as borrowed,
            mock.patch.object(
                server,
                "connect_existing_provider",
                side_effect=AssertionError("should borrow sibling tab"),
            ),
        ):
            Path(td, "app.py").write_text("print('hello')\n", encoding="utf-8")
            reports = server._run_project_audit(
                project=td,
                selected_provider=selected,
                selected_provider_id="deepseek",
                task="Review this project",
            )

        self.assertEqual([report.text for report in reports], ["audit report"])
        borrowed.assert_called_once_with("mimo", selected.session.page)
        advisor.new_chat.assert_called_once_with()
        advisor.close.assert_called_once_with()


class RunSnapshotTests(unittest.TestCase):
    def test_state_has_no_unused_legacy_event_queue(self) -> None:
        self.assertFalse(hasattr(server.State(), "events"))

    def test_reserve_run_is_atomic(self) -> None:
        state = server.State()
        barrier = threading.Barrier(8)
        results = []

        def reserve() -> None:
            barrier.wait()
            results.append(state.reserve_run(
                session_id="session-1",
                project=None,
                task="hello",
                provider_id="deepseek",
            ))

        threads = [threading.Thread(target=reserve) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        accepted = [item for item in results if item is not None]
        self.assertEqual(len(accepted), 1)
        self.assertTrue(state.busy)
        self.assertEqual(state.active_run, accepted[0])

    def test_stop_after_reservation_survives_task_start(self) -> None:
        state = server.State()
        state.stop_flag.set()
        run = state.reserve_run(
            session_id="session-1",
            project=None,
            task="hello",
            provider_id="deepseek",
        )
        self.assertIsNotNone(run)
        self.assertFalse(state.stop_flag.is_set())
        assert run is not None

        state.stop_flag.set()
        self.assertTrue(state.start_run(run.run_id))

        self.assertTrue(state.stop_flag.is_set())

    def test_state_snapshot_keeps_pending_action_and_terminal_event(self) -> None:
        state = server.State()
        run = state.reserve_run(
            session_id="session-1",
            project="E:/demo",
            task="test",
            provider_id="qwen",
        )
        self.assertIsNotNone(run)
        assert run is not None
        self.assertTrue(state.start_run(run.run_id))
        pending = {
            "type": "shell_request",
            "run_id": run.run_id,
            "session_id": run.session_id,
            "id": "shell-1",
            "command": "pytest",
            "cwd": ".",
        }
        state.pending_shell["shell-1"] = {"ui_event": pending}
        terminal = {
            "type": "task_done",
            "session_id": run.session_id,
            "stop_reason": "approval",
            "summary": "",
        }

        self.assertTrue(state.finish_run(run.run_id, terminal))
        payload = state.run_state_payload()

        self.assertFalse(payload["busy"])
        self.assertEqual(payload["run_id"], run.run_id)
        self.assertEqual(payload["pending_event"], pending)
        self.assertEqual(payload["last_terminal_event"]["run_id"], run.run_id)

    def test_state_snapshot_restores_active_teaching_card(self) -> None:
        state = server.State()
        run = state.reserve_run(
            session_id="session-1",
            project=None,
            task="hello",
            provider_id="deepseek",
        )
        self.assertIsNotNone(run)
        assert run is not None
        state.start_run(run.run_id)
        teaching = {
            "type": "teach_request",
            "run_id": run.run_id,
            "session_id": run.session_id,
            "id": "teach-1",
            "text": "Click the message box",
        }
        state.pending_teach["teach-1"] = {"ui_event": teaching}

        payload = state.run_state_payload()

        self.assertTrue(payload["busy"])
        self.assertEqual(payload["pending_event"], teaching)

    def test_active_run_does_not_restore_an_unrelated_old_card(self) -> None:
        state = server.State()
        state.pending_shell["old"] = {"ui_event": {
            "type": "shell_request",
            "run_id": "run_old",
            "session_id": "session-old",
            "id": "old",
        }}
        run = state.reserve_run(
            session_id="session-new",
            project=None,
            task="hello",
            provider_id="deepseek",
        )
        self.assertIsNotNone(run)

        self.assertIsNone(state.run_state_payload()["pending_event"])

    def test_forgetting_session_clears_only_its_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.State(td)
            state.last_terminal_event = {
                "type": "task_done",
                "run_id": "run_old",
                "session_id": "session-1",
            }
            state.last_summary = "old receipt"
            state.last_stop_reason = "done"
            state.last_shell_result = {
                "type": "shell_result",
                "id": "shell-old",
                "session_id": "session-1",
            }

            state.forget_conversation("session-2")
            self.assertIsNotNone(state.last_terminal_event)
            state.forget_conversation("session-1")

            self.assertIsNone(state.last_terminal_event)
            self.assertIsNone(state.last_shell_result)
            self.assertEqual(state.last_summary, "")
            self.assertEqual(state.last_stop_reason, "")

    def test_state_snapshot_keeps_only_the_latest_shell_result(self) -> None:
        state = server.State()
        first = {
            "type": "shell_result",
            "id": "shell-1",
            "session_id": "session-1",
            "approved": False,
        }
        latest = {**first, "id": "shell-2", "approved": True}

        state.record_shell_result(first)
        state.record_shell_result(latest)

        self.assertEqual(state.run_state_payload()["last_shell_result"], latest)

    def test_submit_task_reserves_before_browser_queue(self) -> None:
        state = server.State()
        with (
            mock.patch.object(server, "STATE", state),
            mock.patch.object(server, "submit_browser_task") as submit,
        ):
            run_id = server._submit_task(
                "session-1", None, "hello", 8, False, "deepseek"
            )
            rejected = server._submit_task(
                "session-2", None, "second", 8, False, "qwen"
            )

        self.assertIsNotNone(run_id)
        self.assertIsNone(rejected)
        self.assertTrue(state.busy)
        self.assertEqual(submit.call_args.args[-1], run_id)


class SessionThreadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.consensus_patch = mock.patch.object(server, "_run_consensus", return_value=None)
        self.consensus_mock = self.consensus_patch.start()
        self.project_audit_patch = mock.patch.object(server, "_run_project_audit", return_value=())
        self.project_audit_mock = self.project_audit_patch.start()

    def tearDown(self) -> None:
        self.project_audit_patch.stop()
        self.consensus_patch.stop()

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
            current_selector='[data-session-teach-current="token"]',
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
        helper.new_chat.assert_called_once()
        self.assertGreater(helper.new_chat.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(
            helper.new_chat.call_args.kwargs["timeout"],
            server.PROFILE_DOCTOR_TIMEOUT,
        )
        helper.send.assert_called_once()
        self.assertGreater(helper.send.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(
            helper.send.call_args.kwargs["timeout"], server.PROFILE_DOCTOR_TIMEOUT
        )
        helper.close.assert_called_once_with()
        self.assertTrue(state.provider_session_changed("qwen", "old-session"))

    def test_profile_doctor_tries_next_model_after_call_failure(self) -> None:
        state = server.State()
        page = mock.Mock()
        first = mock.Mock()
        first.send.side_effect = TimeoutError("failed")
        second = mock.Mock()
        second.send.return_value = '{"candidate_id":"c1"}'
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

        self.assertEqual(selected, "c1")
        self.assertEqual(
            borrowed.call_args_list,
            [mock.call("mimo", page), mock.call("qwen", page)],
        )
        first.send.assert_called_once()
        second.send.assert_called_once()

    def test_profile_doctor_reduces_shared_budget_after_new_chat(self) -> None:
        state = server.State()
        page = mock.Mock()
        helper = mock.Mock()
        helper.send.return_value = '{"candidate_id":"c1"}'
        request = profile_doctor.make_request(
            "deepseek",
            provider_controls.CONTROL_SEND_BUTTON,
            page,
            (Discovery(mock.Mock(), {"tag": "button", "ariaLabel": "Send"}, 50),),
        )

        with (
            mock.patch.object(server, "borrow_open_provider", return_value=helper),
            mock.patch.object(
                server.time,
                "monotonic",
                side_effect=[100.0, 101.0, 105.0, 110.0],
            ),
        ):
            selected = state.handle_profile_doctor(request)

        self.assertEqual(selected, "c1")
        self.assertEqual(helper.new_chat.call_args.kwargs["timeout"], 85.0)
        self.assertEqual(helper.send.call_args.kwargs["timeout"], 80.0)

    def test_profile_doctor_tries_next_model_after_null_decision(self) -> None:
        state = server.State()
        page = mock.Mock()
        first = mock.Mock()
        first.send.return_value = '{"candidate_id":null}'
        second = mock.Mock()
        second.send.return_value = '{"candidate_id":"c1"}'
        request = profile_doctor.make_request(
            "deepseek",
            provider_controls.CONTROL_SEND_BUTTON,
            page,
            (Discovery(mock.Mock(), {"tag": "button", "ariaLabel": "Send"}, 50),),
        )

        with mock.patch.object(
            server, "borrow_open_provider", side_effect=[first, second]
        ):
            selected = state.handle_profile_doctor(request)

        self.assertEqual(selected, "c1")
        first.close.assert_called_once_with()
        second.close.assert_called_once_with()

    def test_profile_doctor_stops_after_all_three_siblings_decline(self) -> None:
        state = server.State()
        page = mock.Mock()
        helpers = [mock.Mock() for _ in range(3)]
        for helper in helpers:
            helper.send.return_value = '{"candidate_id":null}'
        request = profile_doctor.make_request(
            "deepseek",
            provider_controls.CONTROL_SEND_BUTTON,
            page,
            (Discovery(mock.Mock(), {"tag": "button", "ariaLabel": "Send"}, 50),),
        )

        with mock.patch.object(
            server, "borrow_open_provider", side_effect=helpers
        ) as borrowed:
            selected = state.handle_profile_doctor(request)

        self.assertIsNone(selected)
        self.assertEqual(borrowed.call_count, 3)
        for helper in helpers:
            helper.close.assert_called_once_with()

    def test_profile_doctor_honors_task_cancellation_before_borrowing_tab(self) -> None:
        state = server.State()
        request = profile_doctor.make_request(
            "deepseek",
            provider_controls.CONTROL_SEND_BUTTON,
            mock.Mock(),
            (Discovery(mock.Mock(), {"tag": "button", "ariaLabel": "Send"}, 50),),
        )
        event = threading.Event()
        event.set()

        with (
            cancellation.scope(event),
            mock.patch.object(server, "borrow_open_provider") as borrowed,
        ):
            with self.assertRaises(cancellation.TaskCancelled):
                state.handle_profile_doctor(request)

        borrowed.assert_not_called()

    def test_flow_recovery_tries_next_healthy_sibling(self) -> None:
        from codey import provider_flow

        state = server.State()
        page = mock.Mock()
        first = mock.Mock()
        second = mock.Mock()

        def assert_new_chat(*_args, **_kwargs):
            self.assertFalse(provider_controls.can_doctor())
            self.assertFalse(provider_controls.can_teach())

        def fail_send(*_args, **_kwargs):
            self.assertFalse(provider_controls.can_doctor())
            self.assertFalse(provider_controls.can_teach())
            raise TimeoutError("failed")

        def select_send(*_args, **_kwargs):
            self.assertFalse(provider_controls.can_doctor())
            self.assertFalse(provider_controls.can_teach())
            return '{"candidate_id":"f1"}'

        first.new_chat.side_effect = assert_new_chat
        first.send.side_effect = fail_send
        second.new_chat.side_effect = assert_new_chat
        second.send.side_effect = select_send
        request = provider_flow.FlowRecoveryRequest(
            provider_id="deepseek",
            stage=provider_flow.STAGE_COMPLETION,
            trace=({"response_stable": True, "response_nonempty": True},),
            candidates=(
                provider_flow.FlowCandidate(
                    "f1",
                    provider_flow.STAGE_COMPLETION,
                    (provider_flow.PREDICATE_RESPONSE_STABLE,),
                ),
            ),
            page=page,
        )

        with (
            mock.patch.object(
                server,
                "borrow_open_provider",
                side_effect=[first, second],
            ) as borrowed,
            mock.patch.object(provider_controls, "_handler", mock.Mock()),
            mock.patch.object(provider_controls, "_doctor_handler", mock.Mock()),
        ):
            selected = state.handle_flow_recovery(request)

        self.assertEqual(selected, "f1")
        self.assertEqual(
            borrowed.call_args_list,
            [mock.call("mimo", page), mock.call("qwen", page)],
        )
        first.close.assert_called_once_with()
        second.close.assert_called_once_with()

    def test_flow_recovery_honors_stop_before_borrowing_tab(self) -> None:
        from codey import provider_flow

        state = server.State()
        request = provider_flow.FlowRecoveryRequest(
            provider_id="deepseek",
            stage=provider_flow.STAGE_COMPLETION,
            trace=(),
            candidates=(),
            page=mock.Mock(),
        )
        event = threading.Event()
        event.set()

        with (
            cancellation.scope(event),
            mock.patch.object(server, "borrow_open_provider") as borrowed,
        ):
            with self.assertRaises(cancellation.TaskCancelled):
                state.handle_flow_recovery(request)

        borrowed.assert_not_called()

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
        self.assertIs(provider_flow._handler.__self__, state)
        for name in provider_controls._TASK_CONTEXT_FIELDS:
            self.assertFalse(hasattr(provider_controls._context, name), name)

    def test_shell_request_includes_risk_explanation(self) -> None:
        state = server.State()
        events = state.subscribe()
        provider = mock.Mock()
        provider.name = "DeepSeek Web"
        provider.location = "https://chat.deepseek.com/"
        provider.send.return_value = (
            '{"tool":"shell","args":{"path":".","command":"npm install"}}'
        )

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=provider),
            mock.patch.object(server, "connect_existing_provider", side_effect=RuntimeError("not open")),
        ):
            server._run_task("session-1", td, "Set up the project", 8, False, "deepseek")

        pending = next(iter(state.pending_shell.values()))
        self.assertEqual(pending["risk_label"], "dependency_install")
        self.assertIn("download packages", pending["risk_detail"])
        self.assertIn("Post-approval checklist", pending["post_approval_instructions"])

        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        shell_event = next(event for event in emitted if event["type"] == "shell_request")
        self.assertEqual(shell_event["risk_label"], "dependency_install")
        self.assertEqual(shell_event["risk_title"], "Dependency install")
        self.assertIn("install scripts", shell_event["risk_detail"])

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
        tool_started = next(event for event in emitted if event["type"] == "tool_started")
        tool = next(event for event in emitted if event["type"] == "tool")
        done = next(event for event in emitted if event["type"] == "task_done")
        self.assertEqual(tool_started["kind"], "read")
        self.assertEqual(tool_started["path"], "empty.txt")
        self.assertEqual(tool_started["activity"], "Reading empty.txt")
        self.assertEqual(tool_started["tool_id"], tool["tool_id"])
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
            mock.patch.object(server, "agent_run") as agent_run,
        ):
            server._run_task("session-1", None, "First question", 8, False, "deepseek")
            server._run_task("session-1", None, "Follow-up question", 8, False, "deepseek")

        agent_run.assert_not_called()
        first.new_chat.assert_called_once_with()
        second.new_chat.assert_not_called()
        second.send.assert_called_once_with("Follow-up question")

    def test_plain_chat_uses_hidden_consensus_when_advisors_are_available(self) -> None:
        state = server.State()
        events = state.subscribe()
        provider = mock.Mock()
        provider.name = "DeepSeek Web"
        provider.location = "https://chat.deepseek.com/"
        self.consensus_mock.return_value = ConsensusResult("Combined answer", 2)

        with (
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=provider),
            mock.patch.object(server, "agent_run") as agent_run,
        ):
            server._run_task("session-1", None, "Explain a breathing app", 8, False, "deepseek")

        agent_run.assert_not_called()
        provider.new_chat.assert_called_once_with()
        provider.send.assert_not_called()
        self.consensus_mock.assert_called_once()
        consensus_kwargs = self.consensus_mock.call_args.kwargs
        self.assertTrue(consensus_kwargs["draft_first"])
        self.assertEqual(consensus_kwargs.get("owner_prompt", ""), "")
        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        reply = next(event for event in emitted if event["type"] == "reply")
        self.assertEqual(reply["text"], "Combined answer")

    def test_plain_chat_consensus_aggregate_failure_does_not_resend_prompt(self) -> None:
        state = server.State()
        events = state.subscribe()
        provider = mock.Mock()
        provider.name = "DeepSeek Web"
        provider.location = "https://chat.deepseek.com/"
        provider.send.return_value = "unsafe fallback"
        self.consensus_mock.side_effect = RuntimeError("aggregate timed out")

        with (
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=provider),
            mock.patch.object(server, "agent_run") as agent_run,
        ):
            server._run_task("session-1", None, "Explain a breathing app", 8, False, "deepseek")

        agent_run.assert_not_called()
        provider.new_chat.assert_called_once_with()
        provider.send.assert_not_called()
        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        task_done = next(event for event in emitted if event["type"] == "task_done")
        self.assertEqual(task_done["stop_reason"], "error")
        self.assertIn("aggregate timed out", task_done["summary"])

    def test_plain_chat_degraded_consensus_forgets_provider_session(self) -> None:
        state = server.State()
        events = state.subscribe()
        provider = mock.Mock()
        provider.name = "DeepSeek Web"
        provider.location = "https://chat.deepseek.com/"
        self.consensus_mock.return_value = ConsensusResult("Owner draft", 0, True)

        with (
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=provider),
            mock.patch.object(server, "agent_run") as agent_run,
        ):
            server._run_task("session-1", None, "Explain a breathing app", 8, False, "deepseek")

        agent_run.assert_not_called()
        provider.send.assert_not_called()
        self.assertNotIn("deepseek", state.provider_sessions)
        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        reply = next(event for event in emitted if event["type"] == "reply")
        self.assertEqual(reply["text"], "Owner draft")

    def test_plain_chat_followup_consensus_uses_context_not_owner_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as state_home:
            state = server.State(state_home)
            first = mock.Mock()
            first.name = "DeepSeek Web"
            first.location = "https://chat.deepseek.com/"
            first.send.return_value = "First answer"
            second = mock.Mock()
            second.name = "DeepSeek Web"
            second.location = "https://chat.deepseek.com/"
            self.consensus_mock.side_effect = [None, ConsensusResult("Combined follow-up", 1)]

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", side_effect=[first, second]),
                mock.patch.object(server, "agent_run") as agent_run,
            ):
                server._run_task("session-1", None, "Choose a database", 8, False, "deepseek")
                server._run_task("session-1", None, "Add a migration plan", 8, False, "deepseek")

        agent_run.assert_not_called()
        second.send.assert_not_called()
        consensus_kwargs = self.consensus_mock.call_args.kwargs
        self.assertTrue(consensus_kwargs["draft_first"])
        self.assertEqual(consensus_kwargs.get("owner_prompt", ""), "")
        self.assertIn("Choose a database", consensus_kwargs["context"])
        self.assertIn("First answer", consensus_kwargs["context"])
        self.assertNotIn("Current request:", consensus_kwargs["context"])

    def test_recovered_chat_handoff_goes_to_owner_not_advisor_context(self) -> None:
        with tempfile.TemporaryDirectory() as state_home:
            state = server.State(state_home)
            context = state.conversation_for("session-1")
            context.begin_window("deepseek", "chat")
            context.update_snapshot(ConversationSnapshot(
                mode="chat",
                goal="Choose a database",
                provider_id="deepseek",
                latest_user="Choose a database",
                latest_reply="SQLite is enough.",
            ))
            state.save_ui_state({
                "active_id": "session-1",
                "updated_at": 1,
                "revision": 1,
                "sessions": [{
                    "id": "session-1",
                    "title": "Database plan",
                    "messages": [
                        {"type": "user", "text": "Earlier UI detail"},
                        {"type": "asst", "text": "Keep UI-HISTORY-MARKER"},
                        {"type": "user", "text": "Add a migration plan"},
                    ],
                    "terminalRuns": [],
                    "createdAt": 0,
                    "projectId": None,
                    "provider": "deepseek",
                }],
                "projects": [],
            })
            provider = mock.Mock()
            provider.name = "DeepSeek Web"
            provider.location = "https://chat.deepseek.com/"
            self.consensus_mock.return_value = ConsensusResult("Combined answer", 1)

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(server, "agent_run") as agent_run,
            ):
                server._run_task("session-1", None, "Add a migration plan", 8, False, "deepseek")

        agent_run.assert_not_called()
        consensus_kwargs = self.consensus_mock.call_args.kwargs
        self.assertTrue(consensus_kwargs["draft_first"])
        self.assertIn("Choose a database", consensus_kwargs["context"])
        self.assertNotIn("UI-HISTORY-MARKER", consensus_kwargs["context"])
        self.assertIn("recent_visible_conversation", consensus_kwargs["owner_prompt"])
        self.assertIn("UI-HISTORY-MARKER", consensus_kwargs["owner_prompt"])
        self.assertNotIn("User: Add a migration plan", consensus_kwargs["owner_prompt"])

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
                return_value=RunResult("complete", "done", 3, True, True),
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
        self.assertTrue(task_done["changed"])
        self.assertEqual(task_done["changes"]["changed_count"], 2)
        self.assertEqual(len(task_done["changes"]["files"]), 3)
        self.assertEqual(task_done["changes"]["project"], td)

    def test_run_task_recovers_failed_diff_before_review_and_receipt(self) -> None:
        state = server.State()
        events = state.subscribe()
        provider = mock.Mock()
        provider.name = "DeepSeek Web"
        provider.location = "https://chat.deepseek.com/"
        reviewer = mock.Mock()
        reviewer.name = "Xiaomi MiMo Chat"
        reviewer.location = "https://aistudio.xiaomimimo.com/#/c"
        reviewer.send.return_value = (
            '{"verdict":"approved","summary":"Looks good","findings":[]}'
        )
        final_changes = {
            "ok": True,
            "mode": "snapshot",
            "changed_count": 1,
            "files": [{"path": "app.py", "status": "M"}],
            "diff": "diff --git a/app.py b/app.py\n-old\n+new\n",
        }

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=provider),
            mock.patch.object(
                server,
                "agent_run",
                return_value=RunResult("complete", "done", 3, False, True),
            ),
            mock.patch.object(
                server,
                "collect_changes",
                side_effect=[{"ok": False, "error": "snapshot unavailable"}, final_changes],
            ) as collect_changes,
            mock.patch.object(
                server,
                "connect_existing_provider",
                return_value=reviewer,
            ) as connect_review,
        ):
            server._run_task("session-diff-retry", td, "task", 8, False, "deepseek")

        self.assertEqual(collect_changes.call_count, 2)
        connect_review.assert_called_once_with("mimo")
        reviewer.send.assert_called_once()
        review_prompt = reviewer.send.call_args.args[0]
        self.assertIn("app.py", review_prompt)
        self.assertIn(final_changes["diff"], review_prompt)
        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        task_done = next(event for event in emitted if event["type"] == "task_done")
        self.assertTrue(task_done["changed"])
        self.assertEqual(task_done["changes"]["changed_count"], 1)
        self.assertEqual(task_done["changes"]["files"], final_changes["files"])
        self.assertEqual(task_done["receipt"]["changed_count"], 1)

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
                return_value=RunResult("complete", "done", 3, False, True),
            ) as agent_run,
            mock.patch.object(
                server,
                "collect_changes",
                return_value=changes,
            ) as collect_changes,
            mock.patch.object(server, "connect_existing_provider", return_value=reviewer) as connect_review,
        ):
            server._run_task("session-1", td, "task", 8, False, "deepseek")

        self.assertEqual(agent_run.call_count, 1)
        collect_changes.assert_called_once()
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
                    RunResult("first pass", "done", 3, False, True),
                    RunResult("review fixed", "done", 2, False, True),
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

    def test_review_repair_uses_writer_failover(self) -> None:
        state = server.State()
        state.provider_failover_order = lambda: ("deepseek", "mimo", "qwen", "glm")
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        reviewer = mock.Mock()
        reviewer.send.return_value = (
            '{"verdict":"changes_requested","summary":"Fix it",'
            '"findings":[{"path":"app.py","issue":"Bug",'
            '"suggested_fix":"Repair it"}]}'
        )
        provider_failure = ProviderActionError(ProviderFailure(
            "DeepSeek",
            "send",
            "",
            "",
            "response missing",
            "now",
            "response_missing",
        ))
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "app.py", "status": "M"}],
            "diff": "diff --git a/app.py b/app.py\n-old\n+new\n",
        }
        repaired_changes = {
            "ok": True,
            "changed_count": 2,
            "files": [
                {"path": "app.py", "status": "M"},
                {"path": "tests/test_app.py", "status": "A"},
            ],
            "diff": "diff --git a/tests/test_app.py b/tests/test_app.py\n+test\n",
        }

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=writer) as get_provider,
            mock.patch.object(
                server,
                "agent_run",
                side_effect=[
                    RunResult("first pass", "done", 2, False, True),
                    provider_failure,
                    RunResult("fixed by sibling", "done", 1, False, True),
                ],
            ) as agent_run,
            mock.patch.object(
                server,
                "collect_changes",
                side_effect=[changes, repaired_changes],
            ) as collect_changes,
            mock.patch.object(server, "connect_existing_provider", return_value=reviewer),
        ):
            server._run_task("session-review-failover", td, "task", 12, False, "deepseek")

        self.assertEqual(agent_run.call_count, 3)
        self.assertEqual(collect_changes.call_count, 2)
        self.assertEqual(get_provider.call_count, 3)
        self.assertFalse(agent_run.call_args_list[1].kwargs["fresh_chat"])
        self.assertTrue(agent_run.call_args_list[2].kwargs["fresh_chat"])
        self.assertTrue(agent_run.call_args_list[2].kwargs["strict_fresh_chat"])
        self.assertEqual(agent_run.call_args_list[2].kwargs["provider_id"], "mimo")
        self.assertEqual(state.last_terminal_event["provider"], "mimo")
        self.assertEqual(state.last_terminal_event["summary"], "fixed by sibling")
        self.assertEqual(state.last_terminal_event["changes"]["changed_count"], 2)
        self.assertEqual(
            state.last_terminal_event["changes"]["files"],
            repaired_changes["files"],
        )

    def test_review_followup_without_changes_preserves_prior_checks_passed(self) -> None:
        state = server.State()
        events = state.subscribe()
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        writer.location = "https://chat.deepseek.com/"
        reviewer = mock.Mock()
        reviewer.name = "Xiaomi MiMo Chat"
        reviewer.location = "https://aistudio.xiaomimimo.com/#/c"
        reviewer.send.return_value = (
            '{"verdict":"changes_requested","summary":"Verify one claim",'
            '"findings":[{"path":"app.py","issue":"Possible issue",'
            '"suggested_fix":"Check before changing"}]}'
        )
        changes = {
            "ok": True,
            "mode": "snapshot",
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
                    RunResult("first pass", "done", 3, True, True),
                    RunResult("review claim was invalid", "done", 2, False, False),
                ],
            ),
            mock.patch.object(server, "collect_changes", return_value=changes),
            mock.patch.object(server, "connect_existing_provider", return_value=reviewer),
        ):
            server._run_task("session-1", td, "task", 20, False, "deepseek")

        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        task_done = next(event for event in emitted if event["type"] == "task_done")
        self.assertEqual(task_done["summary"], "review claim was invalid")
        self.assertTrue(task_done["receipt"]["checks_passed"])
        self.assertEqual(
            task_done["receipt"]["text"],
            "1 file changed · checks passed · restore available",
        )

    def test_review_followup_failed_check_does_not_inherit_prior_checks_passed(self) -> None:
        state = server.State()
        events = state.subscribe()
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        writer.location = "https://chat.deepseek.com/"
        reviewer = mock.Mock()
        reviewer.name = "Xiaomi MiMo Chat"
        reviewer.location = "https://aistudio.xiaomimimo.com/#/c"
        reviewer.send.return_value = (
            '{"verdict":"changes_requested","summary":"Verify one claim",'
            '"findings":[{"path":"app.py","issue":"Possible issue",'
            '"suggested_fix":"Check before changing"}]}'
        )
        changes = {
            "ok": True,
            "mode": "snapshot",
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
                    RunResult("first pass", "done", 3, True, True, True),
                    RunResult("tests failed", "done", 2, False, False, True),
                ],
            ),
            mock.patch.object(server, "collect_changes", return_value=changes),
            mock.patch.object(server, "connect_existing_provider", return_value=reviewer),
        ):
            server._run_task("session-1", td, "task", 20, False, "deepseek")

        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        task_done = next(event for event in emitted if event["type"] == "task_done")
        self.assertEqual(task_done["summary"], "tests failed")
        self.assertFalse(task_done["receipt"]["checks_passed"])
        self.assertEqual(
            task_done["receipt"]["text"],
            "1 file changed · restore available",
        )

    def test_review_followup_no_progress_does_not_inherit_prior_checks_passed(self) -> None:
        state = server.State()
        events = state.subscribe()
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        writer.location = "https://chat.deepseek.com/"
        reviewer = mock.Mock()
        reviewer.name = "Xiaomi MiMo Chat"
        reviewer.location = "https://aistudio.xiaomimimo.com/#/c"
        reviewer.send.return_value = (
            '{"verdict":"changes_requested","summary":"Verify one claim",'
            '"findings":[{"path":"app.py","issue":"Possible issue",'
            '"suggested_fix":"Check before changing"}]}'
        )
        changes = {
            "ok": True,
            "mode": "snapshot",
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
                    RunResult("first pass", "done", 3, True, True, True),
                    RunResult("stopped after no progress", "no_progress", 2, False, False, False),
                ],
            ),
            mock.patch.object(server, "collect_changes", return_value=changes),
            mock.patch.object(server, "connect_existing_provider", return_value=reviewer),
        ):
            server._run_task("session-1", td, "task", 20, False, "deepseek")

        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        task_done = next(event for event in emitted if event["type"] == "task_done")
        self.assertEqual(task_done["stop_reason"], "no_progress")
        self.assertFalse(task_done["receipt"]["checks_passed"])
        self.assertEqual(
            task_done["receipt"]["text"],
            "1 file changed · restore available",
        )

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
                return_value=RunResult("complete", "done", 3, False, True),
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
                return_value=RunResult("complete", "done", 3, False, True),
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

    def test_run_task_suppresses_manual_teaching_during_review(self) -> None:
        state = server.State()
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        writer.location = "https://chat.deepseek.com/"
        reviewer = mock.Mock()
        reviewer.name = "Xiaomi MiMo Chat"
        reviewer.location = "https://aistudio.xiaomimimo.com/#/c"
        states: list[tuple[bool, bool]] = []

        def send_review(_prompt, timeout=None):
            del timeout
            states.append((
                provider_controls.can_teach(),
                provider_controls.can_doctor(),
            ))
            return '{"verdict":"approved","summary":"Looks good","findings":[]}'

        reviewer.send.side_effect = send_review
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
                return_value=RunResult("complete", "done", 3, False, True),
            ),
            mock.patch.object(server, "collect_changes", return_value=changes),
            mock.patch.object(server, "connect_existing_provider", return_value=reviewer),
        ):
            server._run_task("session-1", td, "task", 8, False, "deepseek")

        self.assertEqual(states, [(False, False)])

    def test_run_task_skips_review_when_there_are_no_changes(self) -> None:
        state = server.State()
        events = state.subscribe()
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
                return_value=RunResult(
                    "Start with one guided rhythm.\n\nAdd customization later.",
                    "done",
                    1,
                    False,
                    False,
                ),
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
        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        task_done = next(event for event in emitted if event["type"] == "task_done")
        self.assertEqual(
            task_done["summary"],
            "Start with one guided rhythm.\n\nAdd customization later.",
        )
        self.assertFalse(task_done["changed"])
        self.assertEqual(task_done["receipt"]["changed_count"], 0)
        self.assertNotIn("changes", task_done)

    def test_project_read_only_task_can_use_hidden_consensus_answer(self) -> None:
        state = server.State()
        events = state.subscribe()
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        writer.location = "https://chat.deepseek.com/"
        self.consensus_mock.return_value = ConsensusResult("Combined final answer", 2)

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=writer),
            mock.patch.object(
                server,
                "agent_run",
                return_value=RunResult("Writer draft", "done", 1, False, False),
            ) as agent_run,
            mock.patch.object(
                server,
                "collect_changes",
                return_value={"ok": True, "changed_count": 0, "files": [], "diff": ""},
            ),
            mock.patch.object(server, "connect_existing_provider") as connect_review,
        ):
            Path(td, "app.py").write_text("print('existing')\n", encoding="utf-8")
            server._run_task("session-1", td, "Discuss architecture", 8, False, "deepseek")

        self.consensus_mock.assert_called_once()
        consensus_kwargs = self.consensus_mock.call_args.kwargs
        self.assertFalse(consensus_kwargs.get("draft_first", False))
        self.assertIn("Project Map", consensus_kwargs["context"])
        self.assertIn("app.py", consensus_kwargs["context"])
        self.assertEqual(agent_run.call_args.args[2], "Discuss architecture")
        self.assertIn("Project Map", agent_run.call_args.kwargs["project_map"])
        self.assertIn("app.py", agent_run.call_args.kwargs["project_map"])
        connect_review.assert_not_called()
        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        task_done = next(event for event in emitted if event["type"] == "task_done")
        self.assertEqual(task_done["summary"], "Combined final answer")
        self.assertFalse(task_done["changed"])

    def test_existing_project_uses_read_only_audit_reports_before_writer(self) -> None:
        state = server.State()
        events = state.subscribe()
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        writer.location = "https://chat.deepseek.com/"
        self.project_audit_mock.return_value = (
            ConsensusAdvice("qwen", "Qwen", "Possible bug in app.py."),
        )

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=writer),
            mock.patch.object(
                server,
                "agent_run",
                return_value=RunResult("Writer review", "done", 1, False, False),
            ) as agent_run,
            mock.patch.object(
                server,
                "collect_changes",
                return_value={"ok": True, "changed_count": 0, "files": [], "diff": ""},
            ),
        ):
            Path(td, "app.py").write_text("print('existing')\n", encoding="utf-8")
            server._run_task("session-1", td, "Review this project for bugs", 8, False, "deepseek")

        self.project_audit_mock.assert_called_once()
        self.assertIn("Project Map", self.project_audit_mock.call_args.kwargs["context"])
        self.assertIn("app.py", self.project_audit_mock.call_args.kwargs["context"])
        self.consensus_mock.assert_not_called()
        task = agent_run.call_args.args[2]
        self.assertIn("Private ChangeBrief", task)
        self.assertIn("read-only project audit", task)
        self.assertIn("Possible bug in app.py.", task)
        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        task_done = next(event for event in emitted if event["type"] == "task_done")
        self.assertEqual(task_done["summary"], "Writer review")
        self.assertFalse(task_done["changed"])

    def test_existing_project_audit_failure_degrades_to_writer(self) -> None:
        state = server.State()
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        writer.location = "https://chat.deepseek.com/"
        self.project_audit_mock.side_effect = RuntimeError("audit unavailable")

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=writer),
            mock.patch.object(
                server,
                "agent_run",
                return_value=RunResult("Writer review", "done", 1, False, False),
            ) as agent_run,
            mock.patch.object(
                server,
                "collect_changes",
                return_value={"ok": True, "changed_count": 0, "files": [], "diff": ""},
            ),
        ):
            Path(td, "app.py").write_text("print('existing')\n", encoding="utf-8")
            server._run_task("session-1", td, "Review this project for bugs", 8, False, "deepseek")

        self.project_audit_mock.assert_called_once()
        self.assertEqual(agent_run.call_args.args[2], "Review this project for bugs")

    def test_project_read_only_consensus_failure_keeps_writer_answer(self) -> None:
        state = server.State()
        events = state.subscribe()
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        writer.location = "https://chat.deepseek.com/"
        self.consensus_mock.side_effect = RuntimeError("aggregate timed out")

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=writer),
            mock.patch.object(
                server,
                "agent_run",
                return_value=RunResult("Writer draft", "done", 1, False, False),
            ),
            mock.patch.object(
                server,
                "collect_changes",
                return_value={"ok": True, "changed_count": 0, "files": [], "diff": ""},
            ),
        ):
            Path(td, "app.py").write_text("print('existing')\n", encoding="utf-8")
            server._run_task("session-1", td, "Discuss architecture", 8, False, "deepseek")

        self.consensus_mock.assert_called_once()
        self.assertNotIn("deepseek", state.provider_sessions)
        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        task_done = next(event for event in emitted if event["type"] == "task_done")
        self.assertEqual(task_done["summary"], "Writer draft")
        self.assertEqual(task_done["stop_reason"], "done")

    def test_existing_project_write_task_skips_consensus_and_keeps_review_after_changes(self) -> None:
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
            "files": [{"path": "app.py", "status": "M", "additions": 1, "deletions": 0}],
            "diff": "diff --git a/app.py b/app.py\n+new\n",
        }

        def write_and_finish(*args, **kwargs):
            project_root = Path(args[1])
            (project_root / "tests").mkdir()
            (project_root / "tests" / "test_app.py").write_text(
                "def test_ok():\n    assert True\n",
                encoding="utf-8",
            )
            return RunResult("implemented", "done", 2, True, True)

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=writer),
            mock.patch.object(
                server,
                "agent_run",
                side_effect=write_and_finish,
            ) as agent_run,
            mock.patch.object(server, "collect_changes", return_value=changes),
            mock.patch.object(server, "connect_existing_provider", return_value=reviewer) as review_connect,
        ):
            Path(td, "app.py").write_text("print('existing')\n", encoding="utf-8")
            server._run_task("session-1", td, "Build the feature", 8, False, "deepseek")

        self.consensus_mock.assert_not_called()
        self.assertEqual(agent_run.call_args.args[2], "Build the feature")
        self.assertNotIn("Private ChangeBrief", agent_run.call_args.args[2])
        self.assertIn("Project Map", agent_run.call_args.kwargs["project_map"])
        self.assertIn("app.py", agent_run.call_args.kwargs["project_map"])
        review_connect.assert_called_once_with("mimo")
        self.assertNotIn("Private ChangeBrief", reviewer.send.call_args.args[0])
        self.assertIn("Project Map", reviewer.send.call_args.args[0])
        self.assertIn("tests/test_app.py", reviewer.send.call_args.args[0])
        self.assertIn("never use a path from the Project Map", reviewer.send.call_args.args[0])
        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        review_event = next(event for event in emitted if event["type"] == "review")
        self.assertEqual(review_event["text"], "MiMo approved")

    def test_empty_project_uses_hidden_plan_before_writer(self) -> None:
        state = server.State()
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        writer.location = "https://chat.deepseek.com/"
        self.consensus_mock.return_value = ConsensusResult("Start with the smallest useful app.", 2)

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=writer),
            mock.patch.object(
                server,
                "agent_run",
                return_value=RunResult("implemented", "done", 2, True, True),
            ) as agent_run,
            mock.patch.object(
                server,
                "collect_changes",
                return_value={"ok": True, "changed_count": 0, "files": [], "diff": ""},
            ),
        ):
            Path(td, ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
            server._run_task("session-1", td, "Build a new breathing app", 8, False, "deepseek")

        self.consensus_mock.assert_called_once()
        consensus_kwargs = self.consensus_mock.call_args.kwargs
        self.assertTrue(consensus_kwargs["draft_first"])
        self.assertTrue(consensus_kwargs["plan"])
        self.assertIn("Project Map", consensus_kwargs["context"])
        task = agent_run.call_args.args[2]
        self.assertIn("Private ChangeBrief", task)
        self.assertIn("new-project planning", task)
        self.assertIn("Start with the smallest useful app.", task)
        self.assertTrue(agent_run.call_args.kwargs["fresh_chat"])

    def test_empty_project_change_brief_reaches_writer_and_reviewer(self) -> None:
        state = server.State()
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        writer.location = "https://chat.deepseek.com/"
        reviewer = mock.Mock()
        reviewer.name = "Xiaomi MiMo Chat"
        reviewer.location = "https://aistudio.xiaomimimo.com/#/c"
        reviewer.send.return_value = '{"verdict":"approved","summary":"Looks good","findings":[]}'
        self.consensus_mock.return_value = ConsensusResult("Use app.py and add a smoke test.", 2)
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "app.py", "status": "A", "additions": 10, "deletions": 0}],
            "diff": "diff --git a/app.py b/app.py\n+print('ok')\n",
        }

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=writer),
            mock.patch.object(
                server,
                "agent_run",
                return_value=RunResult("implemented", "done", 2, True, True, True),
            ) as agent_run,
            mock.patch.object(server, "collect_changes", return_value=changes),
            mock.patch.object(server, "connect_existing_provider", return_value=reviewer),
        ):
            server._run_task("session-1", td, "Build a tiny app", 8, False, "deepseek")

        writer_task = agent_run.call_args.args[2]
        review_prompt = reviewer.send.call_args.args[0]
        self.assertIn("Private ChangeBrief", writer_task)
        self.assertIn("Use app.py and add a smoke test.", writer_task)
        self.assertIn("Private ChangeBrief", review_prompt)
        self.assertIn("Project Map", review_prompt)
        self.assertIn("intent is satisfied", review_prompt)
        self.assertIn("Use app.py and add a smoke test.", review_prompt)

    def test_empty_project_plan_failure_opens_fresh_writer_chat(self) -> None:
        state = server.State()
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        writer.location = "https://chat.deepseek.com/"
        self.consensus_mock.side_effect = RuntimeError("aggregate timed out")

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=writer),
            mock.patch.object(
                server,
                "agent_run",
                return_value=RunResult("implemented", "done", 2, True, True),
            ) as agent_run,
            mock.patch.object(
                server,
                "collect_changes",
                return_value={"ok": True, "changed_count": 0, "files": [], "diff": ""},
            ),
        ):
            server._run_task("session-1", td, "Build a new breathing app", 8, False, "deepseek")

        self.consensus_mock.assert_called_once()
        consensus_kwargs = self.consensus_mock.call_args.kwargs
        self.assertTrue(consensus_kwargs["draft_first"])
        self.assertTrue(consensus_kwargs["plan"])
        self.assertEqual(agent_run.call_args.args[2], "Build a new breathing app")
        self.assertTrue(agent_run.call_args.kwargs["fresh_chat"])

    def test_empty_project_detector_skips_directory_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as root_td, tempfile.TemporaryDirectory() as outside_td:
            outside = Path(outside_td)
            outside.joinpath("real.py").write_text("print('outside')\n", encoding="utf-8")
            link = Path(root_td, "linked-outside")
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlink unavailable: {exc}")

            self.assertFalse(_project_has_user_files(root_td))

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
            "action": "task",
            "url": "",
            "title": "",
            "message": "response timed out",
            "kind": "transient",
            "stage": "",
            "time": task_done["provider_failure"]["time"],
        })
        self.assertIsNot(state.last_provider_failure, provider.last_failure)

    def test_run_task_records_connect_failure_without_provider_page(self) -> None:
        state = server.State()
        state.provider_failover_order = lambda: ("qwen", "deepseek", "mimo", "glm")
        events = state.subscribe()

        with (
            mock.patch.object(server, "STATE", state),
            mock.patch.object(
                state,
                "get_provider",
                side_effect=RuntimeError("Edge not reachable"),
            ) as get_provider,
        ):
            server._run_task("session-1", None, "hello", 8, False, "qwen")

        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        task_done = next(event for event in emitted if event["type"] == "task_done")
        failure = task_done["provider_failure"]
        self.assertEqual(
            [call.args[0] for call in get_provider.call_args_list],
            ["qwen", "deepseek", "mimo"],
        )
        self.assertEqual(failure["model"], "MiMo")
        self.assertEqual(failure["action"], "connect")
        self.assertEqual(failure["url"], "")
        self.assertEqual(failure["title"], "")
        self.assertEqual(failure["message"], "Edge not reachable")
        for name in provider_controls._TASK_CONTEXT_FIELDS:
            self.assertFalse(hasattr(provider_controls._context, name), name)

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

    def test_run_task_treats_shared_cancellation_as_stop_and_forgets_provider_session(self) -> None:
        state = server.State()
        events = state.subscribe()
        provider = mock.Mock()
        provider.name = "Qwen Studio"
        provider.location = "https://chat.qwen.ai/"
        state.set_provider_session("qwen", "session-1")

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=provider),
            mock.patch.object(
                server,
                "agent_run",
                side_effect=cancellation.TaskCancelled("task stopped"),
            ),
        ):
            server._run_task("session-1", td, "hello", 8, False, "qwen")

        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        task_done = next(event for event in emitted if event["type"] == "task_done")
        self.assertEqual(task_done["stop_reason"], "stopped")
        self.assertIsNone(task_done["provider_failure"])
        self.assertTrue(state.provider_session_changed("qwen", "session-1"))
        for name in provider_controls._TASK_CONTEXT_FIELDS:
            self.assertFalse(hasattr(provider_controls._context, name), name)

    def test_stopped_agent_result_also_forgets_provider_session(self) -> None:
        state = server.State()
        provider = mock.Mock(name="provider")
        provider.name = "Qwen Studio"
        state.set_provider_session("qwen", "session-1")

        def stopped_agent(*_args, **_kwargs):
            state.stop_flag.set()
            return RunResult("stopped", "stopped", 2, False)

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=provider),
            mock.patch.object(server, "agent_run", side_effect=stopped_agent),
        ):
            server._run_task("session-1", td, "task", 8, False, "qwen")

        self.assertTrue(state.provider_session_changed("qwen", "session-1"))

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

            status, payload = changes.restore_snapshot_changes(root, tracker, ["app.py"])

            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"], payload)
            self.assertEqual(payload["restored"], ["app.py"])
            self.assertEqual(path.read_text(encoding="utf-8"), "old\n")

    def test_restore_snapshot_changes_reports_missing_tracker(self) -> None:
        status, payload = changes.restore_snapshot_changes("E:/missing", None)

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
                return RunResult("complete", "done", 1, True, True)

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
    def test_state_persists_ui_state_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.State(td)
            payload = {
                "active_id": "chat-1",
                "updated_at": 123,
                "revision": 1,
                "sessions": [{
                    "id": "chat-1",
                    "title": "你好",
                    "messages": [],
                    "terminalRuns": [],
                    "createdAt": 0,
                    "projectId": None,
                    "provider": "deepseek",
                }],
                "projects": [],
            }

            state.save_ui_state(payload)

            self.assertEqual(state.load_ui_state(), payload)

    def test_serve_launches_pywebview_window(self) -> None:
        fake_webview = mock.Mock()

        with (
            mock.patch.dict("sys.modules", {"webview": fake_webview}),
            mock.patch.object(server, "CodeyHTTPServer") as httpd_cls,
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
        _, kwargs = fake_webview.start.call_args
        self.assertFalse(kwargs["private_mode"])
        self.assertEqual(kwargs["storage_path"], str(server.DEFAULT_STATE_HOME / "webview"))
        httpd.shutdown.assert_called_once()

    def test_serve_keeps_http_server_available_when_webview_fails(self) -> None:
        fake_webview = mock.Mock()
        fake_webview.start.side_effect = RuntimeError("missing webview runtime")

        with (
            mock.patch.dict("sys.modules", {"webview": fake_webview}),
            mock.patch.object(server, "CodeyHTTPServer") as httpd_cls,
            mock.patch.object(server, "_wait_for_manual_browser", side_effect=KeyboardInterrupt) as fallback,
            mock.patch("builtins.print") as printed,
        ):
            httpd = mock.Mock()
            httpd.server_address = ("127.0.0.1", 43210)
            httpd_cls.return_value = httpd

            server.serve(port=0)

        fake_webview.create_window.assert_called_once()
        fake_webview.start.assert_called_once()
        fallback.assert_called_once()
        self.assertEqual(fallback.call_args.args[0], "http://127.0.0.1:43210/")
        self.assertIn("missing webview runtime", str(fallback.call_args.args[1]))
        printed.assert_any_call("\n[codey] shutting down")
        httpd.shutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
