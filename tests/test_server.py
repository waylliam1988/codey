from __future__ import annotations

import http.client
import json
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codey import (
    __version__,
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
from codey.events import RunEvent, run_event_payload, run_event_ui_payload
from codey.handoff import ConversationSnapshot
from codey.knowledge import KnowledgeNote, KnowledgeStore
from codey.models import ToolCall
from codey.provider_diagnostics import ProviderActionError, ProviderFailure
from codey.provider_discovery import Discovery
from codey.providers.local_openai import LocalEndpoint
from codey.research.pipeline import ResearchIterationRun
from codey.research.runner import ResearchRunResult
from codey.run_ledger import read_ledger
from codey.task_runner import TaskRunner, _project_has_user_files
from codey.tool_runtime import ToolOutcome
from codey.verification_policy import VerificationCandidate


VALID_SHA256 = "a" * 64


def valid_research_report(url: str, conclusion: str = "Helium conclusion.") -> str:
    return (
        "## 结论\n"
        f"- {conclusion} [1]\n\n"
        "## 关键证据\n"
        "- [1] The opened source provides direct support.\n\n"
        "## 反证与限制\n"
        "- 未找到强反证；本轮搜索覆盖了用户问题，新的 primary data 会推翻当前结论。\n\n"
        "## 来源质量\n"
        "- [1] secondary · web · undated · example.com\n\n"
        "## 搜索覆盖\n"
        "- query: helium\n"
        "- opened: Helium source\n"
        "- skipped: none representative\n"
        "- stop: enough for test fixture\n\n"
        "## 来源\n"
        f"[1] Helium source - {url}"
    )


def seed_ghost_style_memory(
    state,
    *,
    session_id: str = "session-ghost",
    run_id: str = "run-ghost",
    project: str = "",
) -> None:
    from codey.ghost.schema import GhostSignal, GhostSignalParseResult

    assert state.ghost_inbox is not None
    assert state.ghost_hebbian is not None
    created = state.ghost_inbox.ingest_signals(
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
        session_id=session_id,
        run_id=run_id,
        project=project,
        user_text="以后先给结论",
    )
    assert len(created) == 1
    state.ghost_hebbian.reinforce_candidate(created[0])


def seed_ghost_continuity(
    state,
    *,
    session_id: str = "session-ghost",
    run_id: str = "run-continuity",
    project: str = "",
    text: str = "Continue bounded local projection work",
) -> None:
    assert state.ghost_continuity is not None
    state.ghost_continuity.sync_from_sources(
        user_focus_excerpt=text,
        session_id=session_id,
        run_id=run_id,
        project=project,
        mode="chat" if not project else "planning",
    )


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
            followup_hints=(
                "Follow-up hints:\n"
                "- The approved command exited with code 0.\n"
                "- Do not claim tests passed until a run tool result shows it."
            ),
        )

        self.assertIn("The user approved and ran this shell command:", prompt)
        self.assertIn("npm install", prompt)
        self.assertIn("Exit code: 0", prompt)
        self.assertIn("Setup Context", prompt)
        self.assertIn("- npm: available", prompt)
        self.assertIn("Post-approval checklist", prompt)
        self.assertIn("trusted local check", prompt)
        self.assertIn("Follow-up hints", prompt)
        self.assertIn("Do not claim tests passed", prompt)
        self.assertLess(prompt.index("Post-approval checklist"), prompt.index("Follow-up hints"))

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

    def test_shell_followup_verification_candidates_are_limited_to_relevant_risks(self) -> None:
        with mock.patch.object(server, "safe_verification_candidates", return_value=("candidate",)) as discover:
            dependency = server._shell_followup_verification_candidates("E:/demo", "dependency_install")
            generic = server._shell_followup_verification_candidates("E:/demo", "generic")

        self.assertEqual(dependency, ("candidate",))
        self.assertEqual(generic, ())
        discover.assert_called_once_with("E:/demo")

    def test_shell_followup_hints_passes_result_setup_and_candidates(self) -> None:
        candidate = VerificationCandidate("npm test", "frontend", "package.json")
        with (
            mock.patch.object(
                server,
                "_shell_followup_verification_candidates",
                return_value=(candidate,),
            ) as discover,
            mock.patch.object(
                server,
                "render_shell_followup",
                return_value="Follow-up hints:\n- ok",
            ) as render,
        ):
            text = server._shell_followup_hints(
                pending={
                    "project": "E:/demo",
                    "risk_label": "dependency_install",
                },
                result={
                    "exit_code": 0,
                    "output": "added packages",
                    "truncated": True,
                },
            )

        self.assertEqual(text, "Follow-up hints:\n- ok")
        discover.assert_called_once_with("E:/demo", "dependency_install")
        data = render.call_args.args[0]
        self.assertEqual(data.risk_label, "dependency_install")
        self.assertEqual(data.exit_code, 0)
        self.assertEqual(data.output, "added packages")
        self.assertTrue(data.truncated)
        self.assertEqual(data.verification_candidates, (candidate,))


class ProviderStatusTests(unittest.TestCase):
    def test_provider_payload_marks_available_models(self) -> None:
        payload = server.provider_payload({"deepseek": True, "stepfun": False})

        by_id = {item["id"]: item for item in payload}
        self.assertTrue(by_id["deepseek"]["available"])
        self.assertFalse(by_id["mimo"]["available"])
        self.assertFalse(by_id["stepfun"]["available"])
        self.assertFalse(by_id["qwen"]["available"])
        self.assertFalse(by_id["glm"]["available"])
        self.assertFalse(by_id["local"]["available"])
        self.assertEqual(set(by_id["deepseek"]), {"id", "label", "available"})

    def test_provider_status_update_only_reports_changed_model(self) -> None:
        payload = server.provider_status_update("deepseek", True)

        self.assertEqual(payload, [{"id": "deepseek", "label": "DeepSeek", "available": True}])

    def test_provider_availability_reads_cdp_tabs_without_connecting(self) -> None:
        with (
            mock.patch.object(
                server,
                "provider_tab_availability",
                return_value={"deepseek": True, "mimo": False, "stepfun": True, "qwen": False, "glm": False},
            ) as detected,
            mock.patch.object(server, "connect_existing_provider") as connected,
        ):
            statuses = server.provider_availability()

        self.assertEqual(
            statuses,
            {"deepseek": True, "mimo": False, "stepfun": True, "qwen": False, "glm": False},
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
                        "stepfun": True,
                        "qwen": True,
                        "glm": True,
                    },
                ),
            ):
                statuses = server.provider_availability()
                reviewers = server.reviewer_candidates("deepseek")

        self.assertFalse(statuses["qwen"])
        self.assertNotIn("qwen", reviewers)
        self.assertNotIn("local", reviewers)
        self.assertIn("stepfun", reviewers)

    def test_reviewer_candidates_keep_ui_terms_out_of_payload(self) -> None:
        state = server.State()
        with mock.patch.object(server, "STATE", state):
            reviewers = server.reviewer_candidates("deepseek")
            payload = server.provider_payload({"deepseek": True, "mimo": True})

        self.assertNotIn("local", reviewers)
        self.assertNotIn("deepseek", reviewers)
        self.assertEqual(reviewers[0], "mimo")
        for item in payload:
            self.assertNotIn("capability", item)
            self.assertNotIn("fit", item)
            self.assertNotIn("profile", item)
            self.assertNotIn("permission_profile", item)

    def test_provider_warmup_emits_filtered_provider_statuses(self) -> None:
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
            events = state.subscribe()

            with mock.patch.object(server, "STATE", state):
                server._run_provider_warmup(
                    runner=lambda: {
                        "deepseek": True,
                        "stepfun": True,
                        "qwen": True,
                        "glm": False,
                    }
                )

        event = events.get_nowait()
        by_id = {item["id"]: item for item in event["providers"]}
        self.assertEqual(event["type"], "providers")
        self.assertTrue(by_id["deepseek"]["available"])
        self.assertTrue(by_id["stepfun"]["available"])

        self.assertFalse(by_id["qwen"]["available"])
        self.assertFalse(by_id["glm"]["available"])
        self.assertFalse(by_id["local"]["available"])

    def test_provider_warmup_failure_does_not_emit_or_raise(self) -> None:
        state = server.State()
        events = state.subscribe()

        def fail() -> dict[str, bool]:
            raise RuntimeError("browser unavailable")

        with mock.patch.object(server, "STATE", state):
            server._run_provider_warmup(runner=fail)

        self.assertTrue(events.empty())

    def test_start_provider_warmup_uses_browser_worker(self) -> None:
        runner = mock.Mock()

        with mock.patch.object(server, "submit_browser_task") as submit:
            result = server._start_provider_warmup(runner=runner)

        self.assertIsNone(result)
        submit.assert_called_once_with(server._run_provider_warmup, runner)

    def test_failover_order_prefers_open_tabs_then_registry_order(self) -> None:
        state = server.State()
        with mock.patch.object(
            server,
            "provider_tab_availability",
            return_value={"qwen": True, "glm": True},
        ):
            order = state.provider_failover_order()

        self.assertEqual(order, ("qwen", "glm", "deepseek", "mimo", "stepfun"))

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

    def test_run_review_includes_safe_review_impact_map(self) -> None:
        state = server.State()
        reviewer = mock.Mock()
        reviewer.send.return_value = (
            '{"verdict":"approved","summary":"Looks good","findings":[]}'
        )
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "src/api.ts", "status": "M"}],
            "diff": "diff --git a/src/api.ts b/src/api.ts\n+export function renamed() {}\n",
        }

        with (
            mock.patch.object(server, "STATE", state),
            mock.patch.object(server, "reviewer_candidates", return_value=("stepfun",)),
            mock.patch.object(server, "connect_existing_provider", return_value=reviewer),
            mock.patch.object(
                server,
                "safe_review_impact_map",
                return_value=(
                    "Review Impact Map (bounded hints; not coverage proof):\n"
                    "- oldName: src/view.ts:2 (call)"
                ),
            ) as impact_map,
        ):
            reviewed = server._run_review(
                session_id="session-1",
                project="E:/demo",
                task="task",
                writer_summary="done",
                changes=changes,
                recent_log="",
                writer_id="deepseek",
            )

        self.assertIsNotNone(reviewed)
        impact_map.assert_called_once_with("E:/demo", changes)
        prompt = reviewer.send.call_args.args[0]
        self.assertIn("Review Impact Map (bounded hints; not coverage proof)", prompt)
        reviewer.close.assert_called_once_with()

    def test_run_review_reuses_precomputed_review_impact_map(self) -> None:
        state = server.State()
        reviewer = mock.Mock()
        reviewer.send.return_value = (
            '{"verdict":"approved","summary":"Looks good","findings":[]}'
        )
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "src/api.ts", "status": "M"}],
            "diff": "diff --git a/src/api.ts b/src/api.ts\n+export function renamed() {}\n",
        }
        impact = (
            "Review Impact Map (bounded hints; not coverage proof):\n"
            "- oldName: src/view.ts:2 (call)"
        )

        with (
            mock.patch.object(server, "STATE", state),
            mock.patch.object(server, "reviewer_candidates", return_value=("stepfun",)),
            mock.patch.object(server, "connect_existing_provider", return_value=reviewer),
            mock.patch.object(server, "safe_review_impact_map") as impact_map,
        ):
            reviewed = server._run_review(
                session_id="session-1",
                project="E:/demo",
                task="task",
                writer_summary="done",
                changes=changes,
                recent_log="",
                writer_id="deepseek",
                review_impact_map=impact,
            )

        self.assertIsNotNone(reviewed)
        impact_map.assert_not_called()
        self.assertIn(impact, reviewer.send.call_args.args[0])
        reviewer.close.assert_called_once_with()

    def test_run_review_treats_empty_precomputed_review_impact_map_as_final(self) -> None:
        state = server.State()
        reviewer = mock.Mock()
        reviewer.send.return_value = (
            '{"verdict":"approved","summary":"Looks good","findings":[]}'
        )
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "src/api.ts", "status": "M"}],
            "diff": "diff --git a/src/api.ts b/src/api.ts\n+export function renamed() {}\n",
        }

        with (
            mock.patch.object(server, "STATE", state),
            mock.patch.object(server, "reviewer_candidates", return_value=("stepfun",)),
            mock.patch.object(server, "connect_existing_provider", return_value=reviewer),
            mock.patch.object(server, "safe_review_impact_map") as impact_map,
        ):
            reviewed = server._run_review(
                session_id="session-1",
                project="E:/demo",
                task="task",
                writer_summary="done",
                changes=changes,
                recent_log="",
                writer_id="deepseek",
                review_impact_map="",
            )

        self.assertIsNotNone(reviewed)
        impact_map.assert_not_called()
        prompt = reviewer.send.call_args.args[0]
        self.assertNotIn("Review Impact Map (bounded hints; not coverage proof)", prompt)
        reviewer.close.assert_called_once_with()

    def test_run_review_uses_self_review_after_external_reviewers_fail(self) -> None:
        state = server.State()
        state.set_provider_session("deepseek", "session-1")
        events = state.subscribe()
        reviewer = mock.Mock()
        reviewer.send.return_value = (
            '{"verdict":"approved","summary":"Looks good","findings":[]}'
        )
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "app.py", "status": "M"}],
            "diff": "diff --git a/app.py b/app.py\n-old\n+new\n",
        }

        with (
            mock.patch.object(server, "STATE", state),
            mock.patch.object(server, "reviewer_candidates", return_value=("stepfun",)),
            mock.patch.object(
                server,
                "connect_existing_provider",
                side_effect=RuntimeError("not open"),
            ) as connect_existing,
            mock.patch.object(
                server,
                "connect_fresh_provider_tab",
                return_value=reviewer,
            ) as connect_self_review,
        ):
            reviewed = server._run_review(
                session_id="session-1",
                project="E:/demo",
                task="task",
                writer_summary="done",
                changes=changes,
                recent_log="",
                writer_id="DeepSeek",
            )

        self.assertIsNotNone(reviewed)
        self.assertEqual(reviewed[0], "deepseek")
        connect_existing.assert_called_once_with("stepfun")
        connect_self_review.assert_called_once_with("deepseek")
        self.assertEqual(state.provider_sessions["deepseek"], "session-1")
        reviewer.new_chat.assert_called_once_with()
        reviewer.close.assert_called_once_with()
        review_event = events.get_nowait()
        self.assertEqual(review_event["text"], "DeepSeek self-review approved")

    def test_run_review_records_prompt_envelope_for_real_sends(self) -> None:
        state = server.State()
        reviewer = mock.Mock()
        reviewer.send.side_effect = [
            "not json",
            '{"verdict":"approved","summary":"Looks good","findings":[]}',
        ]
        trace = mock.Mock()
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [{"path": "app.py", "status": "M"}],
            "diff": "diff --git a/app.py b/app.py\n-old\n+new\n",
        }

        with (
            mock.patch.object(server, "STATE", state),
            mock.patch.object(server, "reviewer_candidates", return_value=("stepfun",)),
            mock.patch.object(server, "connect_existing_provider", return_value=reviewer),
        ):
            reviewed = server._run_review(
                session_id="session-review-trace",
                project="E:/demo",
                task="task",
                writer_summary="done",
                changes=changes,
                recent_log="",
                writer_id="deepseek",
                trace_recorder=trace,
            )

        self.assertIsNotNone(reviewed)
        self.assertEqual(
            [call.args[0] for call in trace.record_prompt_section.call_args_list],
            ["review_prompt", "review_repair_prompt"],
        )
        self.assertTrue(all(call.kwargs["model_visible"] for call in trace.record_prompt_section.call_args_list))
        trace.record_permission_profile.assert_called_once_with("reviewer", phase="review")

    def test_run_review_self_review_cancellation_closes_temp_reviewer(self) -> None:
        state = server.State()
        reviewer = mock.Mock()
        reviewer.send.side_effect = cancellation.TaskCancelled("task stopped")

        with (
            mock.patch.object(server, "STATE", state),
            mock.patch.object(server, "reviewer_candidates", return_value=()),
            mock.patch.object(server, "connect_fresh_provider_tab", return_value=reviewer),
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

        reviewer.close.assert_called_once_with()

    def test_run_review_cancellation_before_self_review_does_not_open_fresh_tab(self) -> None:
        state = server.State()
        event = threading.Event()

        def fail_and_cancel(_provider_id: str):
            event.set()
            raise RuntimeError("not open")

        with (
            cancellation.scope(event),
            mock.patch.object(server, "STATE", state),
            mock.patch.object(server, "reviewer_candidates", return_value=("stepfun",)),
            mock.patch.object(server, "connect_existing_provider", side_effect=fail_and_cancel),
            mock.patch.object(server, "connect_fresh_provider_tab") as connect_self_review,
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

        connect_self_review.assert_not_called()


class TaskRunnerUiEventTests(unittest.TestCase):
    def test_turn_event_preserves_note(self) -> None:
        event = RunEvent.turn_started(17, '{"tool":"done","args":{"answer":"report"}}', note="(done)")

        payload = run_event_ui_payload("run-1", "session-1", event)

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["type"], "turn")
        self.assertEqual(payload["turn"], 17)
        self.assertEqual(payload["note"], "(done)")

    def test_tool_event_preserves_needs_action_status(self) -> None:
        call = ToolCall("knowledge_write", {"title": "Helium"})
        outcome = ToolOutcome(
            "NEEDS_OPEN: open the source before saving this note: https://example.com/helium",
            True,
            presentation={
                "status": "needs_action",
                "result": "NEEDS_OPEN: open the source before saving this note: https://example.com/helium",
            },
        )
        event = RunEvent.tool_finished(2, call, outcome, index=3)

        payload = run_event_ui_payload("run-1", "session-1", event)

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["type"], "tool")
        self.assertEqual(payload["status"], "needs_action")
        self.assertFalse(payload["error"])

    def test_tool_event_empty_status_falls_back_to_error(self) -> None:
        call = ToolCall("knowledge_write", {"title": "Helium"})
        outcome = ToolOutcome(
            "ERROR: failed",
            False,
            presentation={"status": "", "result": "ERROR: failed"},
        )
        event = RunEvent.tool_finished(2, call, outcome, index=3)

        payload = run_event_ui_payload("run-1", "session-1", event)

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["status"], "error")
        self.assertTrue(payload["error"])

    def test_tool_event_tolerates_malformed_managed_output_audit(self) -> None:
        call = ToolCall("run", {"path": ".", "command": "python test.py"})
        outcome = ToolOutcome(
            "exit 0",
            True,
            audit={
                "managed_output": {
                    "handle": "out_0001_bad",
                    "original_bytes": "abc",
                    "stored_bytes": object(),
                    "sha256": object(),
                }
            },
            exit_code=0,
        )
        event = RunEvent.tool_finished(2, call, outcome, index=3)

        event_payload = run_event_payload(event)
        ui_payload = run_event_ui_payload("run-1", "session-1", event)

        self.assertIsNotNone(event_payload)
        self.assertIsNotNone(ui_payload)
        assert event_payload is not None
        assert ui_payload is not None
        for payload in (event_payload, ui_payload):
            self.assertEqual(payload["output_handle"], "out_0001_bad")
            self.assertEqual(payload["output_bytes"], 0)
            self.assertEqual(payload["output_stored_bytes"], 0)
            self.assertEqual(payload["output_sha256"], "")

    def test_tool_event_empties_invalid_managed_output_sha256(self) -> None:
        call = ToolCall("run", {"path": ".", "command": "python test.py"})
        outcome = ToolOutcome(
            "exit 0",
            True,
            audit={
                "managed_output": {
                    "handle": "out_0001_valid",
                    "original_bytes": 10,
                    "stored_bytes": 10,
                    "sha256": "abc\nINJECTED",
                }
            },
            exit_code=0,
        )
        event = RunEvent.tool_finished(2, call, outcome, index=3)

        event_payload = run_event_payload(event)
        ui_payload = run_event_ui_payload("run-1", "session-1", event)

        self.assertIsNotNone(event_payload)
        self.assertIsNotNone(ui_payload)
        assert event_payload is not None
        assert ui_payload is not None
        for payload in (event_payload, ui_payload):
            serialized = json.dumps(payload, ensure_ascii=False)
            self.assertEqual(payload["output_handle"], "out_0001_valid")
            self.assertEqual(payload["output_sha256"], "")
            self.assertNotIn("INJECTED", serialized)

    def test_tool_event_ignores_invalid_managed_output_handle(self) -> None:
        call = ToolCall("run", {"path": ".", "command": "python test.py"})
        outcome = ToolOutcome(
            "exit 0",
            True,
            audit={
                "managed_output": {
                    "handle": "../x",
                    "original_bytes": 10,
                    "stored_bytes": 10,
                    "sha256": VALID_SHA256,
                }
            },
            exit_code=0,
        )
        event = RunEvent.tool_finished(2, call, outcome, index=3)

        event_payload = run_event_payload(event)
        ui_payload = run_event_ui_payload("run-1", "session-1", event)

        self.assertIsNotNone(event_payload)
        self.assertIsNotNone(ui_payload)
        assert event_payload is not None
        assert ui_payload is not None
        for payload in (event_payload, ui_payload):
            self.assertNotIn("output_handle", payload)
            self.assertNotIn("output_bytes", payload)
            self.assertNotIn("output_stored_bytes", payload)
            self.assertNotIn("output_sha256", payload)


class ResearchGraphApiTests(unittest.TestCase):
    def test_research_graph_api_returns_graph_packet(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.State()
            state.knowledge_store = KnowledgeStore(Path(td, "vault"))
            synthesis = KnowledgeNote.create(
                type="synthesis",
                title="Graph research",
                body="Conclusion",
                tags=["graph", "research"],
                sources=["https://example.com/graph"],
                session_id="s1",
            )
            fact = KnowledgeNote.create(
                type="fact",
                title="Graph fact",
                body="Fact",
                tags=["graph fact"],
                sources=["https://example.com/fact"],
                relations=[{"src": "graph", "dst": "graph fact", "kind": "relates"}],
                session_id="s1",
            )
            state.knowledge_store.write_note(synthesis)
            state.knowledge_store.write_note(fact)
            state.knowledge_store.link(synthesis.id, fact.id, "derives")
            httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            host, port = httpd.server_address
            try:
                with mock.patch.object(server, "STATE", state):
                    conn = http.client.HTTPConnection(host, port, timeout=5)
                    path = (
                        "/api/research/graph?session_id=s1"
                        f"&focus={synthesis.id}&depth=3&counterpoint=Missing+primary+data"
                    )
                    conn.request("GET", path)
                    response = conn.getresponse()
                    payload = json.loads(response.read().decode("utf-8"))
                    conn.close()
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)
                state.knowledge_store.close()

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["ok"])
        self.assertIn(payload["graph"]["center_id"], set(payload["graph"]["focus_ids"]))
        self.assertTrue(any(node["kind"] == "concept" for node in payload["graph"]["nodes"]))
        self.assertTrue(any(node["id"] == synthesis.id for node in payload["graph"]["nodes"]))
        self.assertTrue(any(node["kind"] == "source_url" for node in payload["graph"]["nodes"]))
        self.assertTrue(any(node["kind"] == "counterpoint" for node in payload["graph"]["nodes"]))

    def test_research_graph_api_unknown_focus_returns_empty_graph(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.State()
            state.knowledge_store = KnowledgeStore(Path(td, "vault"))
            httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            host, port = httpd.server_address
            try:
                with mock.patch.object(server, "STATE", state):
                    conn = http.client.HTTPConnection(host, port, timeout=5)
                    conn.request("GET", "/api/research/graph?session_id=missing&focus=unknown")
                    response = conn.getresponse()
                    payload = json.loads(response.read().decode("utf-8"))
                    conn.close()
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)
                state.knowledge_store.close()

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["graph"]["nodes"], [])
        self.assertEqual(payload["graph"]["edges"], [])

    def test_research_concept_graph_api_returns_concepts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.State()
            state.knowledge_store = KnowledgeStore(Path(td, "vault"))
            note = KnowledgeNote.create(
                type="note",
                title="War and helium",
                body="B",
                session_id="s1",
                relations=[{"src": "war", "dst": "helium", "kind": "affects"}],
            )
            state.knowledge_store.write_note(note)
            httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            host, port = httpd.server_address
            try:
                with mock.patch.object(server, "STATE", state):
                    conn = http.client.HTTPConnection(host, port, timeout=5)
                    conn.request("GET", "/api/research/concept_graph?session_id=s1")
                    response = conn.getresponse()
                    payload = json.loads(response.read().decode("utf-8"))
                    conn.close()
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=5)
                state.knowledge_store.close()

        self.assertEqual(response.status, 200)
        self.assertTrue(payload["ok"])
        node_ids = {node["id"] for node in payload["graph"]["nodes"]}
        self.assertEqual(node_ids, {"concept:war", "concept:helium"})
        self.assertEqual(payload["graph"]["edges"][0]["kind"], "affects")

    def test_research_concept_graph_api_without_store_is_404(self) -> None:
        state = server.State()
        state.knowledge_store = None
        httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        host, port = httpd.server_address
        try:
            with mock.patch.object(server, "STATE", state):
                conn = http.client.HTTPConnection(host, port, timeout=5)
                conn.request("GET", "/api/research/concept_graph")
                response = conn.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
                conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

        self.assertEqual(response.status, 404)
        self.assertFalse(payload["ok"])


class ResearchServerHelperTests(unittest.TestCase):
    def test_research_graph_response_parses_bounded_query(self) -> None:
        state = server.State()
        state.knowledge_store = object()
        graph = SimpleNamespace(to_dict=lambda: {"nodes": [], "edges": []})

        with (
            mock.patch.object(server, "STATE", state),
            mock.patch.object(server, "UnifiedResearchGraphBuilder") as builder_cls,
        ):
            builder = builder_cls.return_value
            builder.build_for_session.return_value = graph
            status, payload = server._research_graph_response({
                "session_id": ["s1"],
                "focus": ["fact-1"],
                "synthesis_id": ["synthesis-1"],
                "depth": ["9"],
                "limit": ["999"],
                "edge_limit": ["bad"],
                "include_sources": ["false"],
                "counterpoint": ["one,two", "three"],
            })

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "graph": {"nodes": [], "edges": []}})
        builder_cls.assert_called_once_with(state.knowledge_store)
        builder.build_for_session.assert_called_once_with(
            "s1",
            focus_ids=("synthesis-1", "fact-1"),
            depth=3,
            node_limit=200,
            edge_limit=192,
            counterpoints=("one", "two", "three"),
        )

    def test_research_graph_response_reports_unconfigured_store(self) -> None:
        state = server.State()
        state.knowledge_store = None
        with mock.patch.object(server, "STATE", state):
            status, payload = server._research_graph_response({})

        self.assertEqual(status, 404)
        self.assertEqual(payload, {"ok": False, "error": "Research is not configured"})

    def test_research_note_response_validation_and_payload(self) -> None:
        status, payload = server._research_note_response({})
        self.assertEqual(status, 400)
        self.assertEqual(payload, {"ok": False, "error": "id required"})

        with tempfile.TemporaryDirectory() as td:
            state = server.State()
            state.knowledge_store = KnowledgeStore(Path(td, "vault"))
            note = KnowledgeNote.create(
                type="fact",
                title="Fact",
                body="Body",
                sources=["https://example.com"],
                tags=["research"],
            )
            state.knowledge_store.write_note(note)
            try:
                with mock.patch.object(server, "STATE", state):
                    found_status, found_payload = server._research_note_response({"id": [note.id]})
                    missing_status, missing_payload = server._research_note_response({"id": ["missing"]})
            finally:
                state.knowledge_store.close()

        self.assertEqual(found_status, 200)
        self.assertTrue(found_payload["ok"])
        self.assertEqual(found_payload["note"]["id"], note.id)
        self.assertEqual(found_payload["note"]["title"], "Fact")
        self.assertEqual(found_payload["note"]["body"], "Body")
        self.assertEqual(found_payload["note"]["sources"], ["https://example.com"])
        self.assertEqual(found_payload["note"]["type"], "fact")
        self.assertEqual(found_payload["note"]["path"], f"facts/{note.id}.md")
        self.assertEqual(missing_status, 404)
        self.assertEqual(missing_payload, {"ok": False, "error": "note not found"})

    def test_research_note_response_reports_unconfigured_store(self) -> None:
        state = server.State()
        state.knowledge_store = None
        with mock.patch.object(server, "STATE", state):
            status, payload = server._research_note_response({"id": ["n1"]})

        self.assertEqual(status, 404)
        self.assertEqual(payload, {"ok": False, "error": "Research is not configured"})

    def test_research_restore_response_preserves_status_mapping(self) -> None:
        missing_status, missing_payload = server._research_restore_response({})
        self.assertEqual(missing_status, 400)
        self.assertEqual(missing_payload, {"ok": False, "error": "run_id required"})

        state = server.State()
        with mock.patch.object(server, "STATE", state):
            with mock.patch.object(state, "restore_research_changes", return_value={"ok": False, "error": "busy"}):
                failed_status, failed_payload = server._research_restore_response({"run_id": "r1"})
            with mock.patch.object(state, "restore_research_changes", return_value={"ok": True, "restored": []}):
                ok_status, ok_payload = server._research_restore_response({"run_id": "r1"})

        self.assertEqual(failed_status, 409)
        self.assertEqual(failed_payload, {"ok": False, "error": "busy"})
        self.assertEqual(ok_status, 200)
        self.assertEqual(ok_payload, {"ok": True, "restored": []})

    def test_run_submit_response_validation_and_submit_mapping(self) -> None:
        self.assertEqual(server._run_submit_response({"task": "hello", "intent": "bad"}), (400, {"error": "invalid intent"}))
        self.assertEqual(
            server._run_submit_response({"task": "hello", "max_turns": "bad"}),
            (400, {"error": "invalid max_turns"}),
        )
        self.assertEqual(server._run_submit_response({"task": ""}), (400, {"error": "task required"}))
        self.assertEqual(
            server._run_submit_response({"task": "hello", "provider": "missing"}),
            (400, {"error": "unsupported provider: missing"}),
        )

        with mock.patch.object(server, "_submit_task", return_value="run-1") as submit:
            status, payload = server._run_submit_response({
                "task": "hello",
                "session_id": "",
                "provider": "deepseek",
                "max_turns": "999",
                "continue_task": True,
                "intent": "research",
            })

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "run_id": "run-1"})
        submit.assert_called_once_with("default", None, "hello", 500, True, "deepseek", "research")

        self.assertEqual(
            server._run_submit_response({"task": "review diff", "intent": "review"}),
            (400, {"error": "project required for review"}),
        )

        with tempfile.TemporaryDirectory() as td, mock.patch.object(server, "_submit_task", return_value="run-2") as submit:
            review_status, review_payload = server._run_submit_response({
                "task": "review diff",
                "project": td,
                "provider": "deepseek",
                "intent": "review",
            })

        self.assertEqual(review_status, 200)
        self.assertEqual(review_payload, {"ok": True, "run_id": "run-2"})
        submit.assert_called_once_with("default", td, "review diff", server.DEFAULT_MAX_TURNS, False, "deepseek", "review")

        with mock.patch.object(server, "_submit_task", return_value=None):
            busy_status, busy_payload = server._run_submit_response({"task": "hello"})
        with mock.patch.object(server, "_submit_task", side_effect=RuntimeError("boom")):
            error_status, error_payload = server._run_submit_response({"task": "hello"})

        self.assertEqual(busy_status, 409)
        self.assertEqual(busy_payload, {"error": "busy"})
        self.assertEqual(error_status, 500)
        self.assertEqual(error_payload, {"error": "boom"})

    def test_run_details_response_validation_and_payload(self) -> None:
        self.assertEqual(
            server._run_details_response({}),
            (400, {"ok": False, "error": "session_id and run_id required"}),
        )
        with tempfile.TemporaryDirectory() as td:
            state = server.State(Path(td) / "state")
            writer = state.run_ledgers.open(
                run_id="run-details",
                session_id="session-details",
                project="",
                task="hello",
                provider="deepseek",
                mode="chat",
            )
            writer.finish(stop_reason="done", turns=1, max_turns=8, provider="deepseek")
            with mock.patch.object(server, "STATE", state):
                status, payload = server._run_details_response({
                    "session_id": ["session-details"],
                    "run_id": ["run-details"],
                })

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["available"])
        self.assertEqual(payload["details"]["title"], "Run details")
        self.assertIn(
            {"label": "Work", "value": "Chat", "tone": "neutral"},
            payload["details"]["rows"],
        )

    def test_run_details_response_quiet_unavailable_without_stores(self) -> None:
        state = server.State()
        with mock.patch.object(server, "STATE", state):
            status, payload = server._run_details_response({
                "session_id": ["session-missing"],
                "run_id": ["run-missing"],
            })

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["available"])
        self.assertEqual(
            payload["details"]["rows"][0],
            {"label": "Status", "value": "Details unavailable", "tone": "warning"},
        )


class WebAssetTests(unittest.TestCase):
    def test_research_graph_asset_is_whitelisted(self) -> None:
        httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        host, port = httpd.server_address
        try:
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/assets/research_graph.js?v=0.2.11")
            response = conn.getresponse()
            body = response.read().decode("utf-8")
            ctype = response.getheader("Content-Type") or ""
            conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

        self.assertEqual(response.status, 200)
        self.assertIn("application/javascript", ctype)
        self.assertIn("window.CodeyResearchGraph", body)

    def test_unknown_web_asset_returns_404(self) -> None:
        httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        host, port = httpd.server_address
        try:
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/assets/missing.js")
            response = conn.getresponse()
            response.read()
            conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

        self.assertEqual(response.status, 404)

    def test_asset_path_traversal_and_unknown_extension_return_404(self) -> None:
        httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        host, port = httpd.server_address
        statuses = {}
        try:
            for path in ("/assets/../server.py", "/assets/x.txt", "/assets/", "/assets/..%2fserver.py"):
                conn = http.client.HTTPConnection(host, port, timeout=5)
                conn.request("GET", path)
                response = conn.getresponse()
                response.read()
                conn.close()
                statuses[path] = response.status
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

        self.assertEqual(statuses, {path: 404 for path in statuses})

    def test_index_substitutes_version_placeholder(self) -> None:
        httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        host, port = httpd.server_address
        try:
            conn = http.client.HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/")
            response = conn.getresponse()
            body = response.read().decode("utf-8")
            conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)

        self.assertEqual(response.status, 200)
        self.assertNotIn("__CODEY_VERSION__", body)
        self.assertIn(f"?v={server.__version__}", body)

    def test_runtime_version_matches_release_docs(self) -> None:
        readme = Path("README.md").read_text(encoding="utf-8")
        readme_zh = Path("README.zh-CN.md").read_text(encoding="utf-8")
        changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
        changelog_zh = Path("CHANGELOG.zh-CN.md").read_text(encoding="utf-8")

        self.assertEqual(__version__, "0.4.8")
        self.assertIn(f"Version: `{__version__}`", readme)
        self.assertIn(f"版本：`{__version__}`", readme_zh)
        self.assertIn(f"## {__version__} -", changelog)
        self.assertIn(f"## {__version__} -", changelog_zh)


class LocalProviderApiTests(unittest.TestCase):
    def test_empty_api_key_preserves_existing_key_for_probe_and_save(self) -> None:
        httpd = server.CodeyHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        host, port = httpd.server_address
        try:
            with (
                mock.patch.object(server, "load_local_config", return_value={"api_key": "old-secret"}),
                mock.patch.object(
                    server,
                    "probe_local_endpoint",
                    return_value=LocalEndpoint("http://127.0.0.1:1234/v1", ("llama",)),
                ) as probe,
                mock.patch.object(server, "save_local_config") as save,
                mock.patch.object(server, "local_config_payload", return_value={"connected": True}),
            ):
                conn = http.client.HTTPConnection(host, port, timeout=5)
                conn.request(
                    "POST",
                    "/api/local_provider",
                    body=json.dumps({
                        "base_url": "http://127.0.0.1:1234/v1",
                        "model": "llama",
                        "api_key": "",
                    }),
                    headers={"Content-Type": "application/json"},
                )
                response = conn.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
                conn.close()

            self.assertEqual(response.status, 200)
            self.assertTrue(payload["ok"])
            probe.assert_called_once_with("http://127.0.0.1:1234/v1", api_key="old-secret")
            save.assert_called_once_with(
                "http://127.0.0.1:1234/v1",
                "llama",
                None,
            )
        finally:
            httpd.shutdown()
            httpd.server_close()


class ConsensusConnectionTests(unittest.TestCase):
    def test_consensus_borrows_sibling_tab_from_selected_provider(self) -> None:
        state = server.State()
        selected = mock.Mock()
        selected.session.page = object()
        selected.send.return_value = "combined answer"
        advisor = mock.Mock()
        advisor.name = "StepFun"
        advisor.send.return_value = "advisor note"

        with (
            mock.patch.object(server, "STATE", state),
            mock.patch.object(
                server,
                "provider_availability",
                return_value={"deepseek": True, "mimo": False, "stepfun": True, "qwen": False, "glm": False},
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
        borrowed.assert_called_once_with("stepfun", selected.session.page)
        advisor.new_chat.assert_called_once_with()
        advisor.close.assert_called_once_with()
        selected.send.assert_called_once()

    def test_project_audit_borrows_sibling_tab_from_selected_provider(self) -> None:
        state = server.State()
        selected = mock.Mock()
        selected.session.page = object()
        advisor = mock.Mock()
        advisor.name = "StepFun"
        advisor.send.return_value = '{"tool":"done","args":{"summary":"audit report"}}'

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(
                server,
                "provider_availability",
                return_value={"deepseek": True, "mimo": False, "stepfun": True, "qwen": False, "glm": False},
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
        borrowed.assert_called_once_with("stepfun", selected.session.page)
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

    def test_emit_full_subscriber_queue_drops_oldest_and_keeps_latest(self) -> None:
        state = server.State()
        q: queue.Queue[dict] = queue.Queue(maxsize=2)
        with state.lock:
            state.subscribers.append(q)
        try:
            state.emit({"type": "info", "seq": 1})
            state.emit({"type": "info", "seq": 2})
            state.emit({"type": "info", "seq": 3})

            items = [q.get_nowait(), q.get_nowait()]
        finally:
            state.unsubscribe(q)

        self.assertEqual([item["seq"] for item in items], [2, 3])

    def test_state_snapshot_reports_only_restorable_research_runs(self) -> None:
        from codey.knowledge import KnowledgeChanges, KnowledgeNote, KnowledgeStore

        with tempfile.TemporaryDirectory() as td:
            state = server.State(td)
            state.knowledge_store = KnowledgeStore(Path(td, "vault"))
            changes = KnowledgeChanges(state.knowledge_store.root)
            note = KnowledgeNote.create(type="synthesis", title="Run", body="Report", session_id="s1")
            state.knowledge_store.write_note(note, changes=changes)

            state.record_research_changes("run-research", changes)
            before = state.run_state_payload()
            restored = state.restore_research_changes("run-research")
            after = state.run_state_payload()
            state.knowledge_store.close()

        self.assertIn("run-research", before["research_restore_runs"])
        self.assertTrue(restored["ok"])
        self.assertNotIn("run-research", after["research_restore_runs"])

    def test_restore_research_changes_schedules_rebuild_in_background(self) -> None:
        class SlowStore:
            def __init__(self) -> None:
                self.started = threading.Event()
                self.release = threading.Event()

            def rebuild(self) -> None:
                self.started.set()
                self.release.wait(2)

        state = server.State()
        state.knowledge_store = SlowStore()
        state.record_research_changes(
            "run-research",
            SimpleNamespace(
                restore_result=lambda: SimpleNamespace(
                    ok=True,
                    restored=["note.md"],
                    conflicts=[],
                    error="",
                ),
            ),
        )
        done = threading.Event()
        holder: dict[str, dict] = {}

        def restore() -> None:
            holder["payload"] = state.restore_research_changes("run-research")
            done.set()

        thread = threading.Thread(target=restore)
        thread.start()
        try:
            self.assertTrue(state.knowledge_store.started.wait(1))
            self.assertTrue(done.wait(0.2))
            self.assertEqual(holder["payload"]["restored"], ["note.md"])
        finally:
            state.knowledge_store.release.set()
            thread.join(1)

    def test_restore_research_changes_coalesces_background_rebuilds(self) -> None:
        class CoalescingStore:
            def __init__(self) -> None:
                self.lock = threading.Lock()
                self.first_started = threading.Event()
                self.second_started = threading.Event()
                self.first_release = threading.Event()
                self.second_release = threading.Event()
                self.extra_started = threading.Event()
                self.count = 0
                self.active = 0
                self.max_active = 0

            def rebuild(self) -> None:
                with self.lock:
                    self.count += 1
                    index = self.count
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                try:
                    if index == 1:
                        self.first_started.set()
                        self.first_release.wait(2)
                    elif index == 2:
                        self.second_started.set()
                        self.second_release.wait(2)
                    else:
                        self.extra_started.set()
                finally:
                    with self.lock:
                        self.active -= 1

        def change() -> SimpleNamespace:
            return SimpleNamespace(
                restore_result=lambda: SimpleNamespace(
                    ok=True,
                    restored=[],
                    conflicts=[],
                    error="",
                ),
            )

        state = server.State()
        state.knowledge_store = CoalescingStore()
        for run_id in ("run-1", "run-2", "run-3"):
            state.record_research_changes(run_id, change())

        try:
            self.assertTrue(state.restore_research_changes("run-1")["ok"])
            self.assertTrue(state.knowledge_store.first_started.wait(1))
            self.assertTrue(state.restore_research_changes("run-2")["ok"])
            self.assertTrue(state.restore_research_changes("run-3")["ok"])
            state.knowledge_store.first_release.set()
            self.assertTrue(state.knowledge_store.second_started.wait(1))
            self.assertEqual(state.knowledge_store.max_active, 1)
            self.assertEqual(state.knowledge_store.count, 2)
            self.assertFalse(state.knowledge_store.extra_started.is_set())
        finally:
            state.knowledge_store.first_release.set()
            state.knowledge_store.second_release.set()

    def test_restore_research_changes_rebuild_error_does_not_change_restore_result(self) -> None:
        class FailingStore:
            def __init__(self) -> None:
                self.started = threading.Event()

            def rebuild(self) -> None:
                self.started.set()
                raise RuntimeError("index failed")

        state = server.State()
        state.knowledge_store = FailingStore()
        state.record_research_changes(
            "run-research",
            SimpleNamespace(
                restore_result=lambda: SimpleNamespace(
                    ok=True,
                    restored=["note.md"],
                    conflicts=[],
                    error="",
                ),
            ),
        )

        restored = state.restore_research_changes("run-research")

        self.assertTrue(restored["ok"])
        self.assertEqual(restored["restored"], ["note.md"])
        self.assertTrue(state.knowledge_store.started.wait(1))

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

    def test_ghost_summary_api_is_unavailable_without_state_home(self) -> None:
        old_state = server.STATE
        try:
            server.STATE = server.State(None)
            status, payload = server._ghost_summary_response({
                "session_id": ["s1"],
                "project": ["E:/project"],
            })
            action_status, action_payload = server._ghost_action_response({
                "action": "disable_updates",
            })
        finally:
            server.STATE = old_state

        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["available"])
        self.assertEqual(action_status, 200)
        self.assertFalse(action_payload["ok"])

    def test_ghost_summary_api_returns_bounded_local_context(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            old_state = server.STATE
            try:
                state = server.State(td)
                seed_ghost_style_memory(state, session_id="s1", project="")
                server.STATE = state
                status, payload = server._ghost_summary_response({"session_id": ["s1"]})
            finally:
                server.STATE = old_state

        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["counts"]["active"], 1)
        self.assertNotIn("evidence_quote", encoded)

    def test_forgetting_session_clears_only_its_terminal_event(self) -> None:
        from codey.ghost.continuity import build_ghost_continuity
        from codey.ghost.affinity import GhostAffinityStore
        from codey.ghost.router import (
            GhostRouteDecision,
            GhostRouteRequest,
            finalize_route_decision,
        )
        from codey.ghost.work_queue import GhostWorkQueueStore
        from codey.knowledge.research_interest import ResearchInterestCandidate

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
            seed_ghost_continuity(
                state,
                session_id="session-1",
                run_id="run-continuity-1",
                text="Should Session one scoped focus continue?",
            )
            seed_ghost_continuity(
                state,
                session_id="session-2",
                run_id="run-continuity-2",
                text="Should Session two scoped focus continue?",
            )
            assert state.ghost_router is not None
            keep_request = GhostRouteRequest(
                task="keep",
                baseline_mode="chat",
                session_id="session-1",
                run_id="run-router-1",
            )
            delete_request = GhostRouteRequest(
                task="delete",
                baseline_mode="chat",
                session_id="session-2",
                run_id="run-router-2",
            )
            state.ghost_router.append_result(
                finalize_route_decision(
                    keep_request,
                    GhostRouteDecision("research", 0.9, "fresh", True),
                ),
                keep_request,
            )
            state.ghost_router.append_result(
                finalize_route_decision(
                    delete_request,
                    GhostRouteDecision("research", 0.9, "fresh", True),
                ),
                delete_request,
            )
            assert state.ghost_work_queue is not None
            GhostWorkQueueStore(td).sync_from_sources(
                continuity_store=state.ghost_continuity,
                session_id="session-1",
            )
            GhostWorkQueueStore(td).sync_from_sources(
                continuity_store=state.ghost_continuity,
                session_id="session-2",
            )
            assert state.ghost_affinity is not None
            GhostAffinityStore(td).sync_from_sources(
                research_interest_candidates=(
                    ResearchInterestCandidate(
                        id="ric-session-1",
                        question="Research whether one concept should continue",
                        related_concepts=("session-one",),
                        shared_neighbors=(),
                        source_refs=("note:one",),
                        scope="session",
                        scope_ref="session-1",
                        priority=0.7,
                        confidence=0.8,
                        why_now="Bounded server test.",
                        source="concept_open_question",
                        source_ref="concept:one",
                        strong_support=True,
                    ),
                    ResearchInterestCandidate(
                        id="ric-session-2",
                        question="Research whether two concept should continue",
                        related_concepts=("session-two",),
                        shared_neighbors=(),
                        source_refs=("note:two",),
                        scope="session",
                        scope_ref="session-2",
                        priority=0.7,
                        confidence=0.8,
                        why_now="Bounded server test.",
                        source="concept_open_question",
                        source_ref="concept:two",
                        strong_support=True,
                    ),
                ),
                session_id="session-1",
            )

            state.forget_conversation("session-2")
            router_records = state.ghost_router.export_state()["router"]["records"]
            work_items = state.ghost_work_queue.export_state()["work_queue"]["items"]
            affinity_nodes = state.ghost_affinity.export_state()["affinity"]["nodes"]
            self.assertIsNotNone(state.last_terminal_event)
            self.assertEqual([row["session_id"] for row in router_records], ["session-1"])
            self.assertEqual(len(work_items), 1)
            self.assertEqual([row["key"] for row in affinity_nodes], ["session-one"])
            self.assertIn(
                "Session one scoped focus",
                build_ghost_continuity(state.ghost_continuity, session_id="session-1").text,
            )
            self.assertNotIn(
                "Session two scoped focus",
                build_ghost_continuity(state.ghost_continuity, session_id="session-2").text,
            )
            state.forget_conversation("session-1")

            self.assertIsNone(state.last_terminal_event)
            self.assertIsNone(state.last_shell_result)
            self.assertEqual(state.last_summary, "")
            self.assertEqual(state.last_stop_reason, "")
            self.assertNotIn(
                "Session one scoped focus",
                build_ghost_continuity(state.ghost_continuity, session_id="session-1").text,
            )
            self.assertEqual(state.ghost_router.export_state()["router"]["records"], [])
            self.assertEqual(state.ghost_work_queue.export_state()["work_queue"]["items"], [])
            self.assertEqual(state.ghost_affinity.export_state()["affinity"]["nodes"], [])

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

    def test_chat_mode_prepends_ghost_directive(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.State(td)
            seed_ghost_style_memory(state, session_id="session-ghost")
            seed_ghost_continuity(state, session_id="session-ghost")
            events = state.subscribe()
            provider = mock.Mock()
            provider.name = "DeepSeek Web"
            provider.location = "https://chat.deepseek.com/"
            provider.send.return_value = "direct reply"

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
            ):
                server._run_task(
                    "session-ghost",
                    None,
                    "Answer this directly",
                    8,
                    False,
                    "deepseek",
                    "chat",
                )

            emitted = []
            while not events.empty():
                emitted.append(events.get_nowait())

        prompt = provider.send.call_args.args[0]
        done = next(event for event in emitted if event["type"] == "task_done")
        self.assertIn("Local Context:", prompt)
        self.assertNotIn("Ghost", prompt)
        self.assertIn("reply structure = answer first", prompt)
        self.assertIn("Continue bounded local projection work", prompt)
        self.assertNotIn("Prefer concise answer-first replies.", prompt)
        self.assertEqual(done["mode"], "chat")

    def test_chat_mode_runs_post_turn_ghost_learning_after_task_done(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.State(td)
            events = state.subscribe()
            provider = mock.Mock()
            provider.name = "DeepSeek Web"
            provider.location = "https://chat.deepseek.com/"
            provider.send.return_value = "normal reply"
            learning_provider = mock.Mock()
            learning_provider.send.return_value = (
                '{"signals":[{'
                '"kind":"style_preference",'
                '"scope":"user",'
                '"summary":"Prefer concise replies.",'
                '"evidence_quote":"以后回答短一点",'
                '"confidence":0.94,'
                '"metadata":{"conflict_key":"reply_length","value_key":"concise"}'
                '}]}'
            )
            state.ghost_learning_provider_factory = mock.Mock(return_value=learning_provider)

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
            ):
                server._run_task(
                    "session-learn",
                    None,
                    "以后回答短一点",
                    8,
                    False,
                    "deepseek",
                    "chat",
                )

            emitted = []
            while not events.empty():
                emitted.append(events.get_nowait())
            assert state.ghost_hebbian is not None
            from codey.ghost.directive import build_ghost_directive

            directive_text = build_ghost_directive(state.ghost_hebbian).text

        event_types = [event["type"] for event in emitted]
        self.assertLess(event_types.index("task_done"), event_types.index("ghost_learning_done"))
        self.assertLess(event_types.index("ghost_learning_done"), event_types.index("ghost_continuity_done"))
        learning_event = next(event for event in emitted if event["type"] == "ghost_learning_done")
        continuity_event = next(event for event in emitted if event["type"] == "ghost_continuity_done")
        self.assertTrue(learning_event["ok"])
        self.assertEqual(learning_event["accepted_count"], 1)
        self.assertEqual(learning_event["reinforced_count"], 1)
        self.assertTrue(continuity_event["ok"])
        self.assertGreaterEqual(continuity_event["items_changed"], 1)
        self.assertIn("User message:", learning_provider.send.call_args.args[0])
        self.assertNotIn("User message:", provider.send.call_args.args[0])
        self.assertIn("reply length = concise", directive_text)

    def test_chat_ghost_disable_skips_post_turn_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.State(td)
            assert state.ghost_inbox is not None
            state.ghost_inbox.set_learning_enabled(False)
            events = state.subscribe()
            provider = mock.Mock()
            provider.name = "DeepSeek Web"
            provider.location = "https://chat.deepseek.com/"
            provider.send.return_value = "normal reply"
            state.ghost_learning_provider_factory = mock.Mock(return_value=mock.Mock())

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
            ):
                server._run_task(
                    "session-disabled",
                    None,
                    "以后回答短一点",
                    8,
                    False,
                    "deepseek",
                    "chat",
                )

            emitted = []
            while not events.empty():
                emitted.append(events.get_nowait())

        state.ghost_learning_provider_factory.assert_not_called()
        learning_event = next(event for event in emitted if event["type"] == "ghost_learning_done")
        continuity_event = next(event for event in emitted if event["type"] == "ghost_continuity_done")
        self.assertEqual(learning_event["skipped_reason"], "learning_disabled")
        self.assertEqual(continuity_event["skipped_reason"], "learning_disabled")

    def test_state_kicks_ghost_sleep_without_sse_event(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.State(td)
            events = state.subscribe()
            started = threading.Event()
            assert state.ghost_sleep is not None
            state.ghost_sleep.run_once = mock.Mock(side_effect=lambda **_kwargs: started.set())

            started_ok = state.kick_ghost_sleep(run_id="r1", session_id="s1")

            self.assertTrue(started_ok)
            self.assertTrue(started.wait(2))
            emitted = []
            while not events.empty():
                emitted.append(events.get_nowait())

        self.assertEqual(emitted, [])

    def test_state_ghost_sleep_is_single_flight(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.State(td)
            started = threading.Event()
            release = threading.Event()
            active = 0
            max_active = 0
            calls = 0
            lock = threading.Lock()

            def run_once(**_kwargs):
                nonlocal active, calls, max_active
                with lock:
                    active += 1
                    calls += 1
                    max_active = max(max_active, active)
                started.set()
                release.wait(2)
                with lock:
                    active -= 1

            assert state.ghost_sleep is not None
            state.ghost_sleep.run_once = mock.Mock(side_effect=run_once)

            first = state.kick_ghost_sleep(run_id="r1", session_id="s1")
            self.assertTrue(started.wait(2))
            second = state.kick_ghost_sleep(run_id="r2", session_id="s1")
            release.set()
            for _ in range(50):
                with state.lock:
                    running = state._ghost_sleep_running
                if not running:
                    break
                threading.Event().wait(0.02)

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertGreaterEqual(calls, 1)
        self.assertEqual(max_active, 1)

    def test_pending_ghost_sleep_uses_latest_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.State(td)
            seen: list[dict[str, object]] = []

            def run_once(**kwargs):
                seen.append(kwargs)
                if len(seen) == 1:
                    state.kick_ghost_sleep(
                        trigger="post_turn",
                        run_id="run-2",
                        session_id="session-2",
                        project="project-2",
                        run_projection={"run": 2},
                    )

            assert state.ghost_sleep is not None
            state.ghost_sleep.run_once = mock.Mock(side_effect=run_once)

            kicked = state.kick_ghost_sleep(
                trigger="post_turn",
                run_id="run-1",
                session_id="session-1",
                project="project-1",
                run_projection={"run": 1},
            )
            state.wait_for_ghost_sleep(timeout=2)

        self.assertTrue(kicked)
        self.assertEqual([row["run_id"] for row in seen], ["run-1", "run-2"])
        self.assertEqual(seen[1]["session_id"], "session-2")
        self.assertEqual(seen[1]["project"], "project-2")
        self.assertEqual(seen[1]["run_projection"], {"run": 2})

    def test_ghost_disable_blocks_auto_sleep(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.State(td)
            assert state.ghost_inbox is not None
            assert state.ghost_sleep is not None
            state.ghost_inbox.set_learning_enabled(False)
            state.ghost_sleep.run_once = mock.Mock()

            kicked = state.kick_ghost_sleep(run_id="r1", session_id="s1")

        self.assertFalse(kicked)
        state.ghost_sleep.run_once.assert_not_called()

    def test_task_done_kicks_ghost_sleep_without_sleep_event(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.State(td)
            events = state.subscribe()
            provider = mock.Mock()
            provider.name = "DeepSeek Web"
            provider.location = "https://chat.deepseek.com/"
            provider.send.return_value = "normal reply"

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(state, "kick_ghost_sleep") as kick_sleep,
            ):
                server._run_task(
                    "session-sleep",
                    None,
                    "hello",
                    8,
                    False,
                    "deepseek",
                    "chat",
                )

            emitted = []
            while not events.empty():
                emitted.append(events.get_nowait())

        kick_sleep.assert_called_once()
        self.assertNotIn("ghost_sleep_done", [event["type"] for event in emitted])

    def test_planning_readonly_does_not_run_ghost_learning_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.State(td)
            provider = mock.Mock()
            provider.name = "DeepSeek Web"
            provider.location = "https://chat.deepseek.com/"
            state.ghost_learning_provider_factory = mock.Mock(return_value=mock.Mock())

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(
                    server,
                    "agent_run",
                    return_value=RunResult("planned", "done", 1),
                ),
            ):
                server._run_task(
                    "session-plan-no-learn",
                    td,
                    "Plan only",
                    8,
                    False,
                    "deepseek",
                    "planning",
                )

        state.ghost_learning_provider_factory.assert_not_called()

    def test_chat_consensus_receives_ghost_directive_only_as_owner_prompt(self) -> None:
        self.consensus_mock.return_value = ConsensusResult("consensus reply", 1)
        with tempfile.TemporaryDirectory() as td:
            state = server.State(td)
            seed_ghost_style_memory(state, session_id="session-ghost")
            seed_ghost_continuity(state, session_id="session-ghost")
            provider = mock.Mock()
            provider.name = "DeepSeek Web"
            provider.location = "https://chat.deepseek.com/"

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
            ):
                server._run_task(
                    "session-ghost",
                    None,
                    "Answer this directly",
                    8,
                    False,
                    "deepseek",
                    "chat",
                )

        kwargs = self.consensus_mock.call_args.kwargs
        self.assertIn("Local Context:", kwargs["owner_prompt"])
        self.assertNotIn("Ghost", kwargs["owner_prompt"])
        self.assertIn("reply structure = answer first", kwargs["owner_prompt"])
        self.assertIn("Continue bounded local projection work", kwargs["owner_prompt"])
        self.assertNotIn("Prefer concise answer-first replies.", kwargs["owner_prompt"])
        self.assertNotIn("Local Context:", kwargs["context"])
        self.assertNotIn("Ghost", kwargs["context"])
        self.assertNotIn("Continue bounded local projection work", kwargs["context"])

    def test_planning_readonly_passes_ghost_directive_to_agent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.State(td)
            seed_ghost_style_memory(state, session_id="session-plan", project=td)
            seed_ghost_continuity(
                state,
                session_id="session-plan",
                project=td,
                text="Plan bounded continuity projection",
            )
            provider = mock.Mock()
            provider.name = "DeepSeek Web"
            provider.location = "https://chat.deepseek.com/"

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(
                    server,
                    "agent_run",
                    return_value=RunResult("planned", "done", 1),
                ) as agent_run,
            ):
                server._run_task(
                    "session-plan",
                    td,
                    "Plan only",
                    8,
                    False,
                    "deepseek",
                    "planning",
                )

        self.assertEqual(agent_run.call_args.kwargs["permission_profile"], "planning_readonly")
        self.assertIn("Local Context:", agent_run.call_args.kwargs["ghost_directive"])
        self.assertNotIn("Ghost", agent_run.call_args.kwargs["ghost_directive"])
        self.assertIn("Local Context:", agent_run.call_args.kwargs["ghost_continuity"])
        self.assertIn("Plan bounded continuity projection", agent_run.call_args.kwargs["ghost_continuity"])
        self.assertNotIn("Ghost", agent_run.call_args.kwargs["ghost_continuity"])

    def test_project_writer_receives_empty_ghost_directive(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = server.State(td)
            seed_ghost_style_memory(state, session_id="session-project", project=td)
            provider = mock.Mock()
            provider.name = "DeepSeek Web"
            provider.location = "https://chat.deepseek.com/"

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(
                    server,
                    "agent_run",
                    return_value=RunResult("done", "done", 1),
                ) as agent_run,
            ):
                server._run_task(
                    "session-project",
                    td,
                    "Build the feature",
                    8,
                    False,
                    "deepseek",
                    "project",
                )

        self.assertEqual(agent_run.call_args.kwargs["permission_profile"], "coding_writer")
        self.assertEqual(agent_run.call_args.kwargs["ghost_directive"], "")
        self.assertEqual(agent_run.call_args.kwargs["ghost_continuity"], "")

    def test_state_marks_provider_available_after_connect(self) -> None:
        class FakeProvider:
            name = "StepFun Chat"
            location = "https://chat.stepfun.com/chats/"

        state = server.State()
        events = state.subscribe()
        with mock.patch.object(server, "connect_provider", return_value=FakeProvider()):
            state.get_provider("stepfun")

        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        providers = next(event for event in emitted if event["type"] == "providers")
        self.assertEqual(providers["providers"], [{"id": "stepfun", "label": "StepFun", "available": True}])

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
        state.set_provider_session("stepfun", "old-session")
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
            [mock.call("mimo", page), mock.call("stepfun", page)],
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
        self.assertTrue(state.provider_session_changed("stepfun", "old-session"))

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
            [mock.call("mimo", page), mock.call("stepfun", page)],
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
            [mock.call("mimo", page), mock.call("stepfun", page)],
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

    def test_research_unavailable_fallback_uses_capability_order(self) -> None:
        state = server.State()
        state.provider_failover_order = lambda: ("deepseek", "mimo", "stepfun")
        state.provider_supervisor.record_failure(
            "deepseek",
            ProviderFailure("DeepSeek", "send", "", "", "limited", "now", "rate_limited"),
        )
        provider = mock.Mock()
        provider.name = "StepFun Chat"
        provider.location = "https://chat.stepfun.com/chats/"

        with (
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=provider) as get_provider,
            mock.patch.object(
                TaskRunner,
                "_run_research_iteration",
                return_value=ResearchIterationRun(result=ResearchRunResult("question", "summary", "done", 1)),
            ) as research_task,
        ):
            server._run_task(
                "session-research-fallback",
                None,
                "Research storage",
                8,
                False,
                "deepseek",
                "research",
            )

        get_provider.assert_called_once_with("stepfun")
        self.assertEqual(research_task.call_args.kwargs["provider_id"], "stepfun")
        self.assertEqual(state.last_terminal_event["provider"], "stepfun")

    def test_project_run_retains_truncated_run_output_as_managed_handle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            project.mkdir()
            (project / "large.py").write_text("print('large')\n", encoding="utf-8")
            state = server.State(root / "state")
            provider = mock.Mock()
            provider.name = "DeepSeek Web"
            provider.location = "https://chat.deepseek.com/"
            completed = subprocess.CompletedProcess(
                ["python", "large.py"],
                1,
                stdout=(
                    "HEAD"
                    + ("x" * 200)
                    + "MIDDLE_MANAGED_OUTPUT"
                    + ("y" * 200)
                    + "TAIL"
                ),
                stderr="",
            )

            def fake_agent_run(*_args, **kwargs):
                self.assertEqual(kwargs["permission_profile"], "coding_writer")
                tool_fns = kwargs["tool_fns"]
                self.assertIsNotNone(tool_fns)
                outcome = tool_fns.execute_run_command(
                    project,
                    ".",
                    "python large.py",
                    permission_profile=kwargs["permission_profile"],
                    tool_id="1:0",
                )
                kwargs["on_event"](RunEvent.tool_finished(
                    1,
                    ToolCall("run", {"path": ".", "command": "python large.py"}),
                    outcome,
                ))
                return RunResult("checked", "done", 1, False, False, True)

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(server, "agent_run", side_effect=fake_agent_run),
                mock.patch.object(server, "collect_changes", return_value={"ok": True, "changed_count": 0, "files": []}),
                mock.patch.object(server, "_run_project_audit", return_value=()),
                mock.patch("codey.tool_runtime.RUN_OUTPUT_LIMIT", 80),
                mock.patch("codey.tool_runtime.cancellation.run_process", return_value=completed),
            ):
                server._run_task(
                    "session-managed-output",
                    str(project),
                    "Run the check",
                    8,
                    False,
                    "deepseek",
                )

            run_id = state.last_terminal_event["run_id"]
            rows = [
                item.payload
                for item in read_ledger(
                    state.run_ledgers.path_for("session-managed-output", run_id)
                )
            ]
            run_row = next(
                item
                for item in rows
                if item["type"] == "tool_finished" and item["tool"] == "run"
            )
            handle = run_row["output_handle"]
            self.assertTrue(str(handle).startswith("out_"))
            self.assertGreater(run_row["output_bytes"], 0)
            self.assertGreater(run_row["output_stored_bytes"], 0)
            self.assertRegex(str(run_row["output_sha256"]), r"^[0-9a-f]{64}$")
            saved = state.managed_outputs.path_for(
                "session-managed-output",
                run_id,
                str(handle),
            ).read_text(encoding="utf-8")
            metadata = json.loads(
                state.managed_outputs.metadata_path_for(
                    "session-managed-output",
                    run_id,
                    str(handle),
                ).read_text(encoding="utf-8")
            )
            self.assertIn("MIDDLE_MANAGED_OUTPUT", saved)
            self.assertNotIn("MIDDLE_MANAGED_OUTPUT", run_row["result"])
            self.assertEqual(metadata["tool_id"], "1:0")

    def test_hybrid_unavailable_fallback_uses_research_capability_order(self) -> None:
        state = server.State()
        state.provider_failover_order = lambda: ("deepseek", "mimo", "stepfun")
        state.provider_supervisor.record_failure(
            "deepseek",
            ProviderFailure("DeepSeek", "send", "", "", "limited", "now", "rate_limited"),
        )
        provider = mock.Mock()
        provider.name = "StepFun Chat"
        provider.location = "https://chat.stepfun.com/chats/"

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=provider) as get_provider,
            mock.patch.object(
                TaskRunner,
                "_run_research_iteration",
                return_value=ResearchIterationRun(result=ResearchRunResult("question", "summary", "done", 1)),
            ) as research_task,
            mock.patch.object(
                server,
                "agent_run",
                return_value=RunResult("project done", "done", 1, False, False),
            ),
            mock.patch.object(
                server,
                "collect_changes",
                return_value={"ok": True, "changed_count": 0, "files": []},
            ),
            mock.patch.object(server, "_run_project_audit", return_value=()),
        ):
            server._run_task(
                "session-hybrid-fallback",
                td,
                "Research storage and update docs",
                8,
                False,
                "deepseek",
                "hybrid",
            )

        get_provider.assert_called_once_with("stepfun")
        self.assertEqual(research_task.call_args.kwargs["provider_id"], "stepfun")
        self.assertEqual(state.last_terminal_event["provider"], "stepfun")

    def test_research_connect_failure_uses_capability_order(self) -> None:
        state = server.State()
        state.provider_failover_order = lambda: ("deepseek", "mimo", "stepfun")
        provider = mock.Mock()
        provider.name = "StepFun Chat"
        provider.location = "https://chat.stepfun.com/chats/"

        with (
            mock.patch.object(server, "STATE", state),
            mock.patch.object(
                state,
                "get_provider",
                side_effect=[RuntimeError("tab unavailable"), provider],
            ) as get_provider,
            mock.patch.object(
                TaskRunner,
                "_run_research_iteration",
                return_value=ResearchIterationRun(result=ResearchRunResult("question", "summary", "done", 1)),
            ) as research_task,
        ):
            server._run_task(
                "session-research-connect-fallback",
                None,
                "Research storage",
                8,
                False,
                "deepseek",
                "research",
            )

        self.assertEqual(
            [call.args[0] for call in get_provider.call_args_list],
            ["deepseek", "stepfun"],
        )
        self.assertEqual(research_task.call_args.kwargs["provider_id"], "stepfun")
        self.assertEqual(state.last_terminal_event["provider"], "stepfun")

    def test_research_fallback_does_not_block_only_avoid_provider(self) -> None:
        state = server.State()
        state.provider_failover_order = lambda: ("mimo",)
        state.provider_supervisor.record_failure(
            "deepseek",
            ProviderFailure("DeepSeek", "send", "", "", "limited", "now", "rate_limited"),
        )
        provider = mock.Mock()
        provider.name = "MiMo Chat"
        provider.location = "https://kimi.moonshot.cn/"

        with (
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=provider) as get_provider,
            mock.patch.object(
                TaskRunner,
                "_run_research_iteration",
                return_value=ResearchIterationRun(result=ResearchRunResult("question", "summary", "done", 1)),
            ) as research_task,
        ):
            server._run_task(
                "session-research-avoid-only",
                None,
                "Research storage",
                8,
                False,
                "deepseek",
                "research",
            )

        get_provider.assert_called_once_with("mimo")
        self.assertEqual(research_task.call_args.kwargs["provider_id"], "mimo")
        self.assertEqual(state.last_terminal_event["provider"], "mimo")

    def test_writer_failover_uses_project_capability_order(self) -> None:
        state = server.State()
        state.provider_failover_order = lambda: ("stepfun", "glm")
        first = mock.Mock()
        first.name = "DeepSeek Web"
        first.location = "https://chat.deepseek.com/"
        second = mock.Mock()
        second.name = "GLM Chat"
        second.location = "https://chat.z.ai/"
        failure = ProviderActionError(ProviderFailure(
            "DeepSeek",
            "send",
            "",
            "",
            "missing",
            "now",
            "response_missing",
        ))

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(
                state,
                "get_provider",
                side_effect=[first, second],
            ) as get_provider,
            mock.patch.object(
                server,
                "agent_run",
                side_effect=[failure, RunResult("done", "done", 1, False, False)],
            ),
            mock.patch.object(
                server,
                "collect_changes",
                return_value={"ok": True, "changed_count": 0, "files": []},
            ),
            mock.patch.object(server, "_run_project_audit", return_value=()),
            mock.patch(
                "codey.task_runner.rank_providers",
                return_value=("glm", "stepfun"),
            ) as rank,
        ):
            server._run_task(
                "session-project-ranked-failover",
                td,
                "Inspect app.py",
                8,
                False,
                "deepseek",
            )

        self.assertEqual(
            [call.args[0] for call in get_provider.call_args_list],
            ["deepseek", "glm"],
        )
        rank.assert_called_with(("stepfun", "glm"), mode="project")
        self.assertEqual(state.last_terminal_event["provider"], "glm")

    def test_hybrid_writer_failover_uses_project_capability_order(self) -> None:
        state = server.State()
        state.provider_failover_order = lambda: ("mimo", "stepfun")
        first = mock.Mock()
        first.name = "DeepSeek Web"
        first.location = "https://chat.deepseek.com/"
        second = mock.Mock()
        second.name = "StepFun Chat"
        second.location = "https://chat.stepfun.com/chats/"
        failure = ProviderActionError(ProviderFailure(
            "DeepSeek",
            "send",
            "",
            "",
            "missing",
            "now",
            "response_missing",
        ))

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(
                state,
                "get_provider",
                side_effect=[first, second],
            ) as get_provider,
            mock.patch.object(
                TaskRunner,
                "_run_research_iteration",
                return_value=ResearchIterationRun(result=ResearchRunResult("question", "summary", "done", 1)),
            ) as research_task,
            mock.patch.object(
                server,
                "agent_run",
                side_effect=[failure, RunResult("done", "done", 1, False, False)],
            ),
            mock.patch.object(
                server,
                "collect_changes",
                return_value={"ok": True, "changed_count": 0, "files": []},
            ),
            mock.patch.object(server, "_run_project_audit", return_value=()),
            mock.patch(
                "codey.task_runner.rank_providers",
                return_value=("stepfun", "mimo"),
            ) as rank,
        ):
            server._run_task(
                "session-hybrid-ranked-writer-failover",
                td,
                "Research then inspect app.py",
                8,
                False,
                "deepseek",
                "hybrid",
            )

        self.assertEqual(
            [call.args[0] for call in get_provider.call_args_list],
            ["deepseek", "stepfun"],
        )
        self.assertEqual(research_task.call_args.kwargs["provider_id"], "deepseek")
        rank.assert_called_with(("mimo", "stepfun"), mode="project")
        self.assertEqual(state.last_terminal_event["provider"], "stepfun")

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

    def test_run_task_with_research_intent_uses_research_runner(self) -> None:
        class Search:
            def search(self, query, limit=8):
                return [{
                    "title": "Helium source",
                    "url": "https://example.com/helium",
                    "snippet": "Helium data",
                }]

            def fetch(self, url):
                return {
                    "url": url,
                    "title": "Helium source",
                    "text": "Helium is separated from natural gas.",
                    "truncated": False,
                }

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as td:
            state = server.State(td)
            seed_ghost_style_memory(state, session_id="session-research")
            from codey.knowledge import KnowledgeStore
            state.knowledge_store = KnowledgeStore(Path(td, "vault"))
            conversation = state.conversation_for("session-research")
            conversation.begin_window("deepseek", "chat")
            conversation.update_snapshot(ConversationSnapshot(
                mode="chat",
                goal="Choose the storage layer",
                provider_id="deepseek",
                latest_user="SQLite or a flat file?",
                latest_reply="SQLite is better once querying matters.",
            ))
            events = state.subscribe()
            provider = mock.Mock()
            provider.name = "DeepSeek Web"
            provider.location = "https://chat.deepseek.com/"
            provider.send.side_effect = [
                json.dumps({"tool": "web_search", "args": {"query": "helium"}}),
                json.dumps({"tool": "open_result", "args": {"result_id": "r1"}}),
                json.dumps({
                    "tool": "knowledge_write",
                    "args": {
                        "type": "fact",
                        "title": "Helium fixture source",
                        "body": "Helium is separated from natural gas.",
                        "sources": ["s1"],
                    },
                }),
                json.dumps({
                    "tool": "done",
                    "args": {"answer": valid_research_report("https://example.com/helium", "Helium data are sufficient for this fixture.")},
                }),
            ]

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch("codey.task_runner.BrowserSearchProvider", return_value=Search()),
                mock.patch.object(server, "_run_research_advisors", None),
                mock.patch.object(server, "agent_run") as agent_run,
            ):
                server._run_task("session-research", None, "Research helium", 8, False, "deepseek", "research")

            emitted = []
            while not events.empty():
                emitted.append(events.get_nowait())
            done = next(event for event in emitted if event["type"] == "task_done")
            tool_kinds = [event["kind"] for event in emitted if event["type"] == "tool"]
            state.knowledge_store.close()
        self.assertEqual(done["mode"], "research")
        self.assertEqual(done["stop_reason"], "done")
        self.assertTrue(done["research"]["synthesis_id"])
        self.assertIn("read", tool_kinds)
        self.assertIn("citation_map", done["research"])
        self.assertTrue(done["research"]["citation_map"])
        self.assertIn("coverage", done["research"])
        research_intro = provider.send.call_args_list[0].args[0]
        self.assertIn("Conversation context from this chat", research_intro)
        self.assertIn("Choose the storage layer", research_intro)
        self.assertNotIn("Local Context:", research_intro)
        self.assertNotIn("Ghost", research_intro)
        agent_run.assert_not_called()

    def test_research_ui_path_reads_pdf_and_recovers_bad_excerpt(self) -> None:
        html_url = "https://www.aljazeera.com/news/2026/4/21/iran-us-war-four-scenarios-for-whats-next-as-talks-stumble"
        pdf_url = "https://cmenaf.org/wp-content/uploads/report.pdf"

        class Search:
            def search(self, query, limit=8):
                return [
                    {
                        "title": "PDF report",
                        "url": pdf_url,
                        "snippet": "A PDF result that the browser text reader cannot ingest.",
                    },
                    {
                        "title": "Iran-US war: Four scenarios for what's next as talks stumble",
                        "url": html_url,
                        "snippet": "Al Jazeera outlines four possible paths after talks stumble.",
                    },
                ]

            def fetch(self, url):
                if url == pdf_url:
                    return {
                        "url": url,
                        "title": "PDF report",
                        "text": "",
                        "content_kind": "pdf",
                        "mime_type": "application/pdf",
                        "bytes": b"%PDF fixture",
                        "truncated": False,
                    }
                return {
                    "url": html_url,
                    "title": "Iran-US war: Four scenarios for what's next as talks stumble",
                    "text": (
                        "Al Jazeera describes four possible paths after talks stumble. "
                        "The article frames military escalation, diplomatic reset, proxy conflict, "
                        "and a managed stalemate as possible scenarios."
                    ),
                    "truncated": False,
                }

            def close(self):
                pass

        report = (
            "## 1. 结论\n"
            "- PDF 来源可读时应直接作为页码级证据进入报告 [1 p.1]\n\n"
            "## 2. 关键证据\n"
            "- [1 p.1] PDF 页面文本提供了四种后续情景的可读材料。\n\n"
            "## 3. 反证与限制\n"
            "- 未找到强反证；本轮覆盖了 PDF 与 HTML 搜索结果，若官方原文或谈判公告更新，会推翻当前结论。\n\n"
            "## 4. 来源质量\n"
            "- [1] secondary · data · fresh · cmenaf.org\n\n"
            "## 5. 搜索覆盖\n"
            "- query: 2026 US Iran war predictions\n"
            "- opened: PDF p.1\n"
            "- skipped: HTML result after PDF answered the fixture\n"
            "- stop: one representative readable PDF source is enough for this UX fixture\n\n"
            "## 6. 来源\n"
            f"[1] [PDF report]({pdf_url})"
        )

        with tempfile.TemporaryDirectory() as td:
            state = server.State(td)
            from codey.knowledge import KnowledgeStore
            state.knowledge_store = KnowledgeStore(Path(td, "vault"))
            events = state.subscribe()
            provider = mock.Mock()
            provider.name = "Local"
            provider.location = "http://localhost:1234"
            provider.send.side_effect = [
                json.dumps({"tool": "web_search", "args": {"query": "2026 US Iran war predictions"}}),
                json.dumps({"tool": "open_result", "args": {"result_id": "r1"}}),
                json.dumps({
                    "tool": "knowledge_write",
                    "args": {
                        "type": "fact",
                        "title": "PDF report is readable",
                        "body": "The PDF report describes four possible paths after talks stumble.",
                        "sources": ["s1"],
                        "evidence": [{
                            "claim": "The PDF report describes four possible paths after talks stumble.",
                            "source_url": "s1",
                            "excerpt": "This sentence is not present in the opened page.",
                            "stance": "supports",
                            "page": 1,
                        }],
                    },
                }),
                json.dumps({"tool": "done", "args": {"answer": report}}),
            ]

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch("codey.task_runner.BrowserSearchProvider", return_value=Search()),
                mock.patch.object(server, "agent_run") as agent_run,
                mock.patch.dict(sys.modules, {
                    "pypdf": SimpleNamespace(
                        PdfReader=lambda _stream: SimpleNamespace(pages=[
                            SimpleNamespace(
                                extract_text=lambda: (
                                    "The PDF report describes four possible paths after talks stumble. "
                                    "It frames military escalation, diplomatic reset, proxy conflict, "
                                    "and a managed stalemate as possible scenarios."
                                ),
                                get_contents=lambda: [],
                            )
                        ])
                    )
                }),
            ):
                server._run_task("session-research-ux", None, "Research Iran-US scenarios", 8, False, "local", "research")

            emitted = []
            while not events.empty():
                emitted.append(events.get_nowait())
            state.knowledge_store.close()

        done = next(event for event in emitted if event["type"] == "task_done")
        tool_events = [event for event in emitted if event["type"] == "tool"]
        pdf_event = next(event for event in tool_events if event["path"] == pdf_url)
        note_event = next(event for event in tool_events if event["kind"] == "note")

        self.assertEqual(done["mode"], "research")
        self.assertEqual(done["stop_reason"], "done")
        self.assertEqual(pdf_event["status"], "ok")
        self.assertFalse(pdf_event["error"])
        self.assertIn("PDF report", pdf_event["result"])
        self.assertEqual(note_event["status"], "ok")
        self.assertFalse(note_event["error"])
        self.assertIn("WARNING:", note_event["result"])
        self.assertEqual(done["research"]["citation_map"][0]["url"], pdf_url)
        self.assertEqual(done["research"]["citation_map"][0]["pages"], [1])
        self.assertEqual(done["research"]["opened_sources"][0]["content_kind"], "pdf")
        self.assertEqual(done["research"]["opened_sources"][0]["pages_read"], [1])
        self.assertEqual(done["research"]["evidence_items"][0]["source_url"], pdf_url)
        self.assertEqual(done["research"]["evidence_items"][0]["page"], 1)
        self.assertIn("The PDF report describes four possible paths", done["research"]["evidence_items"][0]["excerpt"])
        agent_run.assert_not_called()

    def test_followup_research_intent_includes_previous_research_context(self) -> None:
        class Search:
            def search(self, query, limit=8):
                return [{
                    "title": "Storage source",
                    "url": "https://example.com/storage",
                    "snippet": "The storage plan source supports the SQLite-backed plan.",
                }]

            def fetch(self, url):
                return {
                    "url": url,
                    "title": "Helium source",
                    "text": "The storage plan source supports the SQLite-backed plan.",
                    "truncated": False,
                }

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as td:
            state = server.State(td)
            from codey.knowledge import KnowledgeStore
            state.knowledge_store = KnowledgeStore(Path(td, "vault"))
            provider = mock.Mock()
            provider.name = "DeepSeek Web"
            provider.location = "https://chat.deepseek.com/"
            provider.send.side_effect = [
                json.dumps({"tool": "web_search", "args": {"query": "storage plan"}}),
                json.dumps({"tool": "open_result", "args": {"result_id": "r1"}}),
                json.dumps({
                    "tool": "knowledge_write",
                    "args": {
                        "type": "fact",
                        "title": "Storage plan source",
                        "body": "The storage plan source supports the SQLite-backed plan.",
                        "sources": ["s1"],
                    },
                }),
                json.dumps({
                    "tool": "done",
                    "args": {"answer": valid_research_report("https://example.com/storage", "First research summary: prefer the SQLite-backed plan.")},
                }),
                json.dumps({"tool": "web_search", "args": {"query": "storage plan followup"}}),
                json.dumps({"tool": "open_result", "args": {"result_id": "r1"}}),
                json.dumps({
                    "tool": "knowledge_write",
                    "args": {
                        "type": "fact",
                        "title": "Storage plan followup",
                        "body": "The storage plan source supports the SQLite-backed plan.",
                        "sources": ["s1"],
                    },
                }),
                json.dumps({
                    "tool": "done",
                    "args": {"answer": valid_research_report("https://example.com/storage", "Second research summary.")},
                }),
            ]

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch("codey.task_runner.BrowserSearchProvider", return_value=Search()),
                mock.patch.object(server, "_run_research_advisors", None),
                mock.patch.object(server, "agent_run") as agent_run,
            ):
                server._run_task("session-research", None, "Research the storage plan", 4, False, "deepseek", "research")
                server._run_task("session-research", None, "Continue researching that plan", 4, False, "deepseek", "research")

            state.knowledge_store.close()

        second_intro = provider.send.call_args_list[4].args[0]
        self.assertIn("Conversation context from this chat", second_intro)
        self.assertIn("First research summary", second_intro)
        agent_run.assert_not_called()

    def test_hybrid_research_inside_project_includes_project_context(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td, "project")
            project.mkdir()
            project_text = str(project.resolve())
            state = server.State(td)
            from codey.knowledge import KnowledgeStore
            state.knowledge_store = KnowledgeStore(Path(td, "vault"))
            state.set_provider_session("deepseek", "session-hybrid")
            conversation = state.conversation_for("session-hybrid")
            conversation.begin_window("deepseek", "project", project_text)
            conversation.update_snapshot(ConversationSnapshot(
                mode="project",
                goal="Implement the API client",
                project=project_text,
                provider_id="deepseek",
                summary="Use the requests-based client wrapper.",
                latest_user="Build the client here.",
                latest_reply="The wrapper should centralize retries.",
            ))
            provider = mock.Mock()
            provider.name = "DeepSeek Web"
            provider.location = "https://chat.deepseek.com/"
            provider.send.side_effect = ["not json", "still not json", "again not json"]

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(server, "agent_run") as agent_run,
            ):
                server._run_task(
                    "session-hybrid",
                    str(project),
                    "Research that client before editing",
                    3,
                    False,
                    "deepseek",
                    "hybrid",
                )

            state.knowledge_store.close()

        hybrid_intro = provider.send.call_args_list[0].args[0]
        self.assertIn("Conversation context from this chat", hybrid_intro)
        self.assertIn("Implement the API client", hybrid_intro)
        self.assertIn("requests-based client wrapper", hybrid_intro)
        agent_run.assert_not_called()

    def test_hybrid_research_failure_finishes_without_project_writer(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td, "project")
            project.mkdir()
            state = server.State(td)
            state.knowledge_store = KnowledgeStore(Path(td, "vault"))
            events = state.subscribe()
            provider = mock.Mock()
            provider.name = "DeepSeek Web"
            provider.location = "https://chat.deepseek.com/"
            research_result = ResearchRunResult(
                question="Research first",
                summary="Research stopped before project work.",
                stop_reason="no_progress",
                turns=2,
                notes_created=["fact-1"],
                synthesis_id="synthesis-1",
            )

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(
                    TaskRunner,
                    "_run_research_iteration",
                    return_value=ResearchIterationRun(result=research_result),
                ) as research_task,
                mock.patch.object(server, "agent_run") as agent_run,
            ):
                server._run_task(
                    "session-hybrid-fail",
                    str(project),
                    "Research before editing",
                    12,
                    False,
                    "deepseek",
                    "hybrid",
                )

            emitted = []
            while not events.empty():
                emitted.append(events.get_nowait())
            done = next(event for event in emitted if event["type"] == "task_done")
            state.knowledge_store.close()

        research_task.assert_called_once()
        agent_run.assert_not_called()
        self.assertEqual(done["mode"], "research")
        self.assertEqual(done["stop_reason"], "no_progress")
        self.assertEqual(done["research"]["synthesis_id"], "synthesis-1")
        self.assertNotIn("changed", done)

    def test_hybrid_success_runs_project_and_keeps_research_payload(self) -> None:
        changes = {
            "ok": True,
            "mode": "snapshot",
            "changed_count": 0,
            "files": [],
            "diff": "",
        }
        with tempfile.TemporaryDirectory() as td:
            project = Path(td, "project")
            project.mkdir()
            (project / "app.py").write_text("value = 1\n", encoding="utf-8")
            state = server.State(td)
            state.knowledge_store = KnowledgeStore(Path(td, "vault"))
            events = state.subscribe()
            provider = mock.Mock()
            provider.name = "DeepSeek Web"
            provider.location = "https://chat.deepseek.com/"
            research_result = ResearchRunResult(
                question="Research first",
                summary="Use the documented API.",
                stop_reason="done",
                turns=3,
                notes_created=["fact-1"],
                notes_updated=["synthesis-1"],
                synthesis_id="synthesis-1",
            )

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(
                    TaskRunner,
                    "_run_research_iteration",
                    return_value=ResearchIterationRun(result=research_result),
                ) as research_task,
                mock.patch.object(
                    server,
                    "agent_run",
                    return_value=RunResult("project done", "done", 4, True, False),
                ) as agent_run,
                mock.patch.object(server, "collect_changes", return_value=changes),
            ):
                server._run_task(
                    "session-hybrid-ok",
                    str(project),
                    "Research then implement",
                    12,
                    False,
                    "deepseek",
                    "hybrid",
                )

            emitted = []
            while not events.empty():
                emitted.append(events.get_nowait())
            done = next(event for event in emitted if event["type"] == "task_done")
            state.knowledge_store.close()

        research_task.assert_called_once()
        agent_run.assert_called_once()
        self.assertEqual(done["summary"], "project done")
        self.assertEqual(done["stop_reason"], "done")
        self.assertEqual(done["research"]["synthesis_id"], "synthesis-1")
        self.assertEqual(done["research"]["notes_created"], ["fact-1"])
        self.assertIn("receipt", done)

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
        self.assertIn("run_id", reply)
        done = next(event for event in emitted if event["type"] == "task_done")
        self.assertEqual(done["mode"], "chat")
        self.assertEqual(done["summary"], "Combined answer")
        self.assertEqual(done["run_id"], reply["run_id"])

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
        self.assertIn("Local Context:", consensus_kwargs.get("owner_prompt", ""))
        self.assertIn("Choose a database", consensus_kwargs.get("owner_prompt", ""))
        self.assertIn("Choose a database", consensus_kwargs["context"])
        self.assertIn("First answer", consensus_kwargs["context"])
        self.assertNotIn("Current request:", consensus_kwargs["context"])
        self.assertNotIn("Local Context:", consensus_kwargs["context"])

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

    def test_chat_to_project_handoff_reaches_writer(self) -> None:
        with tempfile.TemporaryDirectory() as state_home, tempfile.TemporaryDirectory() as td:
            state = server.State(state_home)
            project = Path(td)
            (project / "app.py").write_text("print('existing')\n", encoding="utf-8")
            context = state.conversation_for("session-1")
            context.begin_window("deepseek", "chat")
            context.update_snapshot(ConversationSnapshot(
                mode="chat",
                goal="Build a small notes app",
                provider_id="deepseek",
                latest_user="Pick storage",
                latest_reply="Use SQLite for simple local persistence.",
            ))
            state.save_ui_state({
                "active_id": "session-1",
                "updated_at": 1,
                "revision": 1,
                "sessions": [{
                    "id": "session-1",
                    "title": "Notes plan",
                    "messages": [
                        {"type": "user", "text": "Pick storage"},
                        {"type": "asst", "text": "Keep WRITER-HANDOFF-MARKER with SQLite."},
                        {"type": "user", "text": "Apply the plan here."},
                    ],
                    "terminalRuns": [],
                    "createdAt": 0,
                    "projectId": "project-1",
                    "provider": "deepseek",
                }],
                "projects": [{
                    "id": "project-1",
                    "name": "notes-app",
                    "path": str(project),
                    "expanded": True,
                    "createdAt": 0,
                }],
            })
            provider = mock.Mock()
            provider.name = "DeepSeek Web"
            provider.location = "https://chat.deepseek.com/"

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(
                    server,
                    "agent_run",
                    return_value=RunResult("Writer done", "done", 1, False, False),
                ) as agent_run,
                mock.patch.object(
                    server,
                    "collect_changes",
                    return_value={"ok": True, "changed_count": 0, "files": [], "diff": ""},
                ),
            ):
                server._run_task(
                    "session-1",
                    str(project),
                    "Apply the plan here.",
                    8,
                    False,
                    "deepseek",
                )

        self.assertEqual(agent_run.call_count, 1)
        self.assertEqual(agent_run.call_args.args[2], "Apply the plan here.")
        self.assertTrue(agent_run.call_args.kwargs["fresh_chat"])
        handoff = agent_run.call_args.kwargs["handoff"]
        self.assertIn("Build a small notes app", handoff)
        self.assertIn("Use SQLite", handoff)
        self.assertIn("recent_visible_conversation", handoff)
        self.assertIn("WRITER-HANDOFF-MARKER", handoff)
        self.assertNotIn("User: Apply the plan here.", handoff)

    def test_restart_after_chat_attach_preserves_project_handoff_for_writer(self) -> None:
        with tempfile.TemporaryDirectory() as state_home, tempfile.TemporaryDirectory() as td:
            project = Path(td)
            (project / "app.py").write_text("print('existing')\n", encoding="utf-8")
            first = server.State(state_home)
            context = first.conversation_for("session-1")
            context.begin_window("deepseek", "chat")
            context.record_exchange(
                "Pick storage",
                "Use SQLite for simple local persistence.",
                ConversationSnapshot(
                    mode="chat",
                    goal="Build a small notes app",
                    provider_id="deepseek",
                    latest_user="Pick storage",
                    latest_reply="Use SQLite for simple local persistence.",
                ),
            )
            first.save_ui_state({
                "active_id": "session-1",
                "updated_at": 1,
                "revision": 1,
                "sessions": [{
                    "id": "session-1",
                    "title": "Notes plan",
                    "messages": [
                        {"type": "user", "text": "Pick storage"},
                        {
                            "type": "asst",
                            "text": "Keep RESTART-ATTACH-MARKER with SQLite.",
                        },
                        {
                            "type": "tool",
                            "kind": "read_file",
                            "result": "secret tool output",
                        },
                        {"type": "shell_result", "output": "secret shell output"},
                        {"type": "user", "text": "Apply the plan here."},
                    ],
                    "terminalRuns": [],
                    "createdAt": 0,
                    "projectId": "project-1",
                    "provider": "deepseek",
                }],
                "projects": [{
                    "id": "project-1",
                    "name": "notes-app",
                    "path": str(project),
                    "expanded": True,
                    "createdAt": 0,
                }],
            })

            restarted = server.State(state_home)
            provider = mock.Mock()
            provider.name = "DeepSeek Web"
            provider.location = "https://chat.deepseek.com/"

            with (
                mock.patch.object(server, "STATE", restarted),
                mock.patch.object(restarted, "get_provider", return_value=provider),
                mock.patch.object(
                    server,
                    "agent_run",
                    return_value=RunResult("Writer done", "done", 1, False, False),
                ) as agent_run,
                mock.patch.object(
                    server,
                    "collect_changes",
                    return_value={"ok": True, "changed_count": 0, "files": [], "diff": ""},
                ),
            ):
                server._run_task(
                    "session-1",
                    str(project),
                    "Apply the plan here.",
                    8,
                    False,
                    "deepseek",
                )

        self.assertEqual(agent_run.call_count, 1)
        self.assertEqual(agent_run.call_args.args[2], "Apply the plan here.")
        self.assertTrue(agent_run.call_args.kwargs["fresh_chat"])
        handoff = agent_run.call_args.kwargs["handoff"]
        self.assertIn("Build a small notes app", handoff)
        self.assertIn("Use SQLite", handoff)
        self.assertIn("recent_visible_conversation", handoff)
        self.assertIn("RESTART-ATTACH-MARKER", handoff)
        self.assertNotIn("User: Apply the plan here.", handoff)
        self.assertNotIn("secret tool output", handoff)
        self.assertNotIn("secret shell output", handoff)

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

    def test_verified_project_run_records_implementation_and_verification_memory(self) -> None:
        changes = {
            "ok": True,
            "mode": "snapshot",
            "changed_count": 1,
            "files": [{"path": "app.py", "status": "M", "additions": 1, "deletions": 1}],
            "diff": "diff --git a/app.py b/app.py\n-old\n+new\n",
        }

        with tempfile.TemporaryDirectory() as td:
            state = server.State(Path(td, "state"))
            state.knowledge_store = KnowledgeStore(Path(td, "vault"))
            project = Path(td, "project")
            project.mkdir()
            (project / "app.py").write_text("old\n", encoding="utf-8")
            synthesis = KnowledgeNote.create(
                type="synthesis",
                title="Research API choice",
                body=(
                    "## 结论\n"
                    "- Use the documented API [1]\n\n"
                    "## 关键证据\n"
                    "- [1] The source documents the API.\n\n"
                    "## 来源\n"
                    "[1] API docs - https://example.com/api"
                ),
                tags=["research", "session:session-memory"],
                sources=["https://example.com/api"],
                session_id="session-memory",
            )
            state.knowledge_store.write_note(synthesis)
            events = state.subscribe()
            provider = mock.Mock()
            provider.name = "DeepSeek Web"
            provider.location = "https://chat.deepseek.com/"

            def fake_agent_run(*_args, **kwargs):
                on_event = kwargs["on_event"]
                on_event(RunEvent.tool_finished(
                    1,
                    ToolCall("edit", {"path": "app.py"}),
                    ToolOutcome("edited", True, changed=True),
                ))
                on_event(RunEvent.tool_finished(
                    2,
                    ToolCall("run", {"command": "python -m unittest", "path": "."}),
                    ToolOutcome("OK", True, exit_code=0),
                ))
                return RunResult("implemented", "done", 2, True, True)

            with (
                mock.patch.object(server, "STATE", state),
                mock.patch.object(state, "get_provider", return_value=provider),
                mock.patch.object(server, "agent_run", side_effect=fake_agent_run),
                mock.patch.object(server, "collect_changes", return_value=changes),
                mock.patch.object(server, "connect_existing_provider", side_effect=RuntimeError("not open")),
            ):
                server._run_task(
                    "session-memory",
                    str(project),
                    "Implement researched API",
                    8,
                    False,
                    "deepseek",
                )

            emitted = []
            while not events.empty():
                emitted.append(events.get_nowait())
            done = next(event for event in emitted if event["type"] == "task_done")
            rows = state.knowledge_store.index.recent(10, session_id="session-memory")
            impl_rows = [row for row in rows if row["type"] == "implementation"]
            verification_rows = [row for row in rows if row["type"] == "verification"]
            impl = state.knowledge_store.read_note(impl_rows[0]["id"])
            verification = state.knowledge_store.read_note(verification_rows[0]["id"])
            links = state.knowledge_store.index.links_for([synthesis.id, impl.id])
            state.knowledge_store.close()

        self.assertEqual(done["stop_reason"], "done")
        self.assertEqual(done["receipt"]["text"], "1 file changed · checks passed · restore available")
        self.assertEqual(len(impl_rows), 1)
        self.assertEqual(len(verification_rows), 1)
        self.assertIsNotNone(impl)
        self.assertIsNotNone(verification)
        assert impl is not None
        assert verification is not None
        self.assertEqual(impl.type, "implementation")
        self.assertEqual(impl.sources, [synthesis.id])
        self.assertIn("Files changed:\n- app.py", impl.body)
        self.assertIn("1 file changed · checks passed", impl.body)
        self.assertEqual(verification.type, "verification")
        self.assertEqual(verification.sources, [impl.id])
        self.assertIn("python -m unittest (cwd .)", verification.body)
        self.assertIn(
            {"src_id": synthesis.id, "dst_id": impl.id, "kind": "implements"},
            links,
        )
        self.assertIn(
            {"src_id": impl.id, "dst_id": verification.id, "kind": "verifies"},
            links,
        )

    def test_run_task_recovers_failed_diff_before_review_and_receipt(self) -> None:
        state = server.State()
        events = state.subscribe()
        provider = mock.Mock()
        provider.name = "DeepSeek Web"
        provider.location = "https://chat.deepseek.com/"
        reviewer = mock.Mock()
        reviewer.name = "StepFun Chat"
        reviewer.location = "https://chat.stepfun.com/chats/"
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
        reviewer.name = "StepFun Chat"
        reviewer.location = "https://chat.stepfun.com/chats/"
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
            mock.patch.object(server, "connect_fresh_provider_tab") as connect_self_review,
        ):
            server._run_task("session-1", td, "task", 8, False, "deepseek")

        self.assertEqual(agent_run.call_count, 1)
        collect_changes.assert_called_once()
        connect_review.assert_called_once_with("mimo")
        connect_self_review.assert_not_called()
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
        reviewer.name = "StepFun Chat"
        reviewer.location = "https://chat.stepfun.com/chats/"
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
        self.assertIn("A review pass inspected the current diff", followup)
        self.assertNotIn("second model reviewed", followup)
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

    def test_run_task_self_review_findings_repair_writer_once(self) -> None:
        state = server.State()
        events = state.subscribe()
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        writer.location = "https://chat.deepseek.com/"
        reviewer = mock.Mock()
        reviewer.name = "DeepSeek Web"
        reviewer.location = "https://chat.deepseek.com/"
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
                    RunResult("self-review fixed", "done", 2, False, True),
                ],
            ) as agent_run,
            mock.patch.object(server, "collect_changes", return_value=changes),
            mock.patch.object(
                server,
                "connect_existing_provider",
                side_effect=RuntimeError("not open"),
            ),
            mock.patch.object(
                server,
                "connect_fresh_provider_tab",
                return_value=reviewer,
            ) as connect_self_review,
        ):
            server._run_task("session-1", td, "task", 20, False, "deepseek")

        connect_self_review.assert_called_once_with("deepseek")
        reviewer.new_chat.assert_called_once_with()
        reviewer.close.assert_called_once_with()
        self.assertEqual(agent_run.call_count, 2)
        followup = agent_run.call_args_list[1].args[2]
        self.assertIn("A review pass inspected the current diff", followup)
        self.assertNotIn("second model reviewed", followup)
        self.assertIn("Missing empty case", followup)
        self.assertFalse(agent_run.call_args_list[1].kwargs["fresh_chat"])
        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        review_event = next(event for event in emitted if event["type"] == "review")
        self.assertEqual(review_event["text"], "DeepSeek self-review suggested changes")
        task_done = next(event for event in emitted if event["type"] == "task_done")
        self.assertEqual(task_done["summary"], "self-review fixed")

    def test_review_repair_uses_writer_failover(self) -> None:
        state = server.State()
        state.provider_failover_order = lambda: ("deepseek", "stepfun", "qwen", "glm")
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
        self.assertEqual(agent_run.call_args_list[2].kwargs["provider_id"], "stepfun")
        self.assertEqual(state.last_terminal_event["provider"], "stepfun")
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
        reviewer.name = "StepFun Chat"
        reviewer.location = "https://chat.stepfun.com/chats/"
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
        reviewer.name = "StepFun Chat"
        reviewer.location = "https://chat.stepfun.com/chats/"
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
        reviewer.name = "StepFun Chat"
        reviewer.location = "https://chat.stepfun.com/chats/"
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
            mock.patch.object(
                server,
                "connect_fresh_provider_tab",
                side_effect=RuntimeError("self-review failed"),
            ) as connect_self_review,
        ):
            server._run_task("session-1", td, "task", 8, False, "deepseek")

        self.assertEqual(agent_run.call_count, 1)
        connect_self_review.assert_called_once_with("deepseek")
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
        reviewer.name = "StepFun Chat"
        reviewer.location = "https://chat.stepfun.com/chats/"
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
        reviewer.name = "StepFun Chat"
        reviewer.location = "https://chat.stepfun.com/chats/"
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
        reviewer.name = "StepFun Chat"
        reviewer.location = "https://chat.stepfun.com/chats/"
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

    def test_review_verification_map_uses_policy_candidate_command_lines(self) -> None:
        state = server.State()
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        writer.location = "https://chat.deepseek.com/"
        reviewer = mock.Mock()
        reviewer.name = "StepFun Chat"
        reviewer.location = "https://chat.stepfun.com/chats/"
        reviewer.send.return_value = '{"verdict":"approved","summary":"Looks good","findings":[]}'
        changes = {
            "ok": True,
            "changed_count": 1,
            "files": [
                {
                    "path": "backend/src/app.ts",
                    "status": "M",
                    "additions": 1,
                    "deletions": 0,
                }
            ],
            "diff": (
                "diff --git a/backend/src/app.ts b/backend/src/app.ts\n"
                "+export const value = 2;\n"
            ),
        }

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=writer),
            mock.patch.object(
                server,
                "agent_run",
                return_value=RunResult("implemented", "done", 1, False, True),
            ),
            mock.patch.object(server, "collect_changes", return_value=changes),
            mock.patch.object(server, "connect_existing_provider", return_value=reviewer),
            mock.patch(
                "codey.verification_policy.shutil.which",
                return_value="exe",
            ),
        ):
            root = Path(td)
            (root / "backend" / "src").mkdir(parents=True)
            (root / "backend" / "src" / "app.ts").write_text(
                "export const value = 1;\n",
                encoding="utf-8",
            )
            (root / "backend" / "package.json").write_text(
                '{"scripts":{"test":"vitest"}}',
                encoding="utf-8",
            )
            (root / "frontend").mkdir()
            (root / "frontend" / "package.json").write_text(
                '{"scripts":{"test":"vitest"}}',
                encoding="utf-8",
            )
            (root / "pnpm-lock.yaml").write_text(
                "lockfileVersion: 9\n",
                encoding="utf-8",
            )
            server._run_task("session-1", td, "Build the feature", 8, False, "deepseek")

        prompt = reviewer.send.call_args.args[0]
        verification_map = prompt.split(
            "Verification Map (bounded candidates; not coverage proof):",
            1,
        )[1].split("Recent tool log:", 1)[0]
        self.assertIn("Candidate commands (inspect before running)", prompt)
        self.assertIn("Recommended local check candidates", prompt)
        self.assertIn("- backend/: pnpm test", verification_map)
        self.assertNotIn("- frontend/: pnpm test", verification_map)

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
        reviewer.name = "StepFun Chat"
        reviewer.location = "https://chat.stepfun.com/chats/"
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
        provider.name = "StepFun"
        provider.location = "https://chat.stepfun.com/chats/"
        provider.last_failure = ProviderFailure(
            model="StepFun",
            action="send",
            url="https://chat.stepfun.com/chats/",
            title="StepFun",
            message="response timed out",
            time="2026-06-28T01:02:03+00:00",
        )

        with (
            tempfile.TemporaryDirectory() as td,
            mock.patch.object(server, "STATE", state),
            mock.patch.object(state, "get_provider", return_value=provider),
            mock.patch.object(server, "agent_run", side_effect=TimeoutError("response timed out")),
        ):
            server._run_task("session-1", td, "task", 8, False, "stepfun")

        emitted = []
        while not events.empty():
            emitted.append(events.get_nowait())
        task_done = next(event for event in emitted if event["type"] == "task_done")
        self.assertEqual(task_done["stop_reason"], "error")
        self.assertEqual(task_done["provider_failure"], {
            "model": "StepFun",
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
        state.provider_failover_order = lambda: ("qwen", "deepseek", "stepfun", "glm")
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
            ["qwen", "deepseek", "stepfun"],
        )
        self.assertEqual(failure["model"], "StepFun")
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
            mock.patch.object(server, "_start_provider_warmup") as warmup,
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
        warmup.assert_called_once_with()
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
            mock.patch.object(server, "_start_provider_warmup") as warmup,
            mock.patch.object(server, "_wait_for_manual_browser", side_effect=KeyboardInterrupt) as fallback,
            mock.patch("builtins.print") as printed,
        ):
            httpd = mock.Mock()
            httpd.server_address = ("127.0.0.1", 43210)
            httpd_cls.return_value = httpd

            server.serve(port=0)

        fake_webview.create_window.assert_called_once()
        fake_webview.start.assert_called_once()
        warmup.assert_called_once_with()
        fallback.assert_called_once()
        self.assertEqual(fallback.call_args.args[0], "http://127.0.0.1:43210/")
        self.assertIn("missing webview runtime", str(fallback.call_args.args[1]))
        printed.assert_any_call("\n[codey] shutting down")
        httpd.shutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
