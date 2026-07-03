from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codey import changes
from tools import bootstrap_smoke


class BootstrapSmokeTests(unittest.TestCase):
    def test_collect_changes_comes_from_changes_module(self) -> None:
        self.assertIs(bootstrap_smoke.collect_changes, changes.collect_changes)

    def test_inject_bug_reverses_snapshot_diff_direction(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "codey").mkdir()
            (root / "codey" / "changes.py").write_text(
                "import difflib\n\n"
                "def _diff_for(path: str, before: str | None, after: str | None) -> str:\n"
                "    old\n"
                "\n\nclass ChangeTracker:\n"
                "    pass\n",
                encoding="utf-8",
            )

            bootstrap_smoke.inject_bug(root)

            text = (root / "codey" / "changes.py").read_text(encoding="utf-8")
            self.assertIn("difflib.unified_diff(after_lines, before_lines", text)

    def test_run_bootstrap_smoke_rejects_same_writer_and_reviewer(self) -> None:
        with self.assertRaisesRegex(ValueError, "reviewer must be different"):
            bootstrap_smoke.run_bootstrap_smoke("deepseek", port=9222, max_turns=4, reviewer_id="deepseek")

    def test_run_bootstrap_smoke_can_use_reviewer(self) -> None:
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        writer.location = "https://chat.deepseek.com/"
        writer.close = mock.Mock()
        reviewer = mock.Mock()
        reviewer.name = "MiMo"
        reviewer.location = "https://aistudio.xiaomimimo.com/#/c"
        reviewer.send.return_value = '{"verdict":"approved","summary":"Looks good","findings":[]}'
        reviewer.close = mock.Mock()

        with (
            mock.patch.object(bootstrap_smoke.provider_controls, "begin_task_context") as begin_context,
            mock.patch.object(bootstrap_smoke.provider_controls, "end_task_context") as end_context,
            mock.patch.object(bootstrap_smoke, "_copy_repo"),
            mock.patch.object(bootstrap_smoke, "inject_bug"),
            mock.patch.object(
                bootstrap_smoke.subprocess,
                "run",
                side_effect=[
                    mock.Mock(returncode=1, stdout="", stderr="FAIL"),
                    mock.Mock(returncode=0, stdout="", stderr="OK"),
                ],
            ),
            mock.patch.object(bootstrap_smoke, "connect_provider", side_effect=[writer, reviewer]) as connect,
            mock.patch.object(bootstrap_smoke, "run", return_value=mock.Mock(stop_reason="done", summary="done", turns=3)),
            mock.patch.object(bootstrap_smoke, "collect_changes", return_value={"ok": True, "changed_count": 1, "files": [], "diff": "+x"}),
            mock.patch.object(bootstrap_smoke.shutil, "rmtree"),
        ):
            data = bootstrap_smoke.run_bootstrap_smoke("deepseek", port=9222, max_turns=8, reviewer_id="mimo")

        self.assertTrue(data["ok"], data)
        begin_context.assert_called_once_with("bootstrap-smoke:deepseek")
        end_context.assert_called_once_with()
        self.assertTrue(data["initial_failure"])
        self.assertEqual(data["review"], "approved")
        self.assertEqual(connect.call_args_list[0], mock.call("deepseek", port=9222))
        self.assertEqual(
            connect.call_args_list[1],
            mock.call("mimo", port=9222, open_if_missing=False, bring_to_front=False),
        )
        reviewer.new_chat.assert_called_once_with()

    def test_run_bootstrap_smoke_cleans_task_context_when_provider_connection_fails(self) -> None:
        with (
            mock.patch.object(bootstrap_smoke, "_copy_repo"),
            mock.patch.object(bootstrap_smoke, "inject_bug"),
            mock.patch.object(
                bootstrap_smoke.subprocess,
                "run",
                return_value=mock.Mock(returncode=1, stdout="", stderr="FAIL"),
            ),
            mock.patch.object(bootstrap_smoke.provider_controls, "begin_task_context") as begin_context,
            mock.patch.object(bootstrap_smoke.provider_controls, "end_task_context") as end_context,
            mock.patch.object(bootstrap_smoke, "connect_provider", side_effect=RuntimeError("offline")),
        ):
            with self.assertRaisesRegex(RuntimeError, "offline"):
                bootstrap_smoke.run_bootstrap_smoke("mimo", port=9222, max_turns=8)

        begin_context.assert_called_once_with("bootstrap-smoke:mimo")
        end_context.assert_called_once_with()

    def test_run_bootstrap_smoke_cleans_task_context_when_provider_close_fails(self) -> None:
        provider = mock.Mock()
        provider.close.side_effect = RuntimeError("CDP disconnected")

        with (
            mock.patch.object(bootstrap_smoke, "_copy_repo"),
            mock.patch.object(bootstrap_smoke, "inject_bug"),
            mock.patch.object(
                bootstrap_smoke.subprocess,
                "run",
                return_value=mock.Mock(returncode=1, stdout="", stderr="FAIL"),
            ),
            mock.patch.object(bootstrap_smoke.provider_controls, "begin_task_context"),
            mock.patch.object(bootstrap_smoke.provider_controls, "end_task_context") as end_context,
            mock.patch.object(bootstrap_smoke, "connect_provider", return_value=provider),
            mock.patch.object(
                bootstrap_smoke,
                "run",
                return_value=mock.Mock(stop_reason="done", summary="done", turns=1),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "CDP disconnected"):
                bootstrap_smoke.run_bootstrap_smoke("qwen", port=9222, max_turns=8)

        end_context.assert_called_once_with()

    def test_run_bootstrap_smoke_requires_initial_failure(self) -> None:
        writer = mock.Mock()
        writer.name = "DeepSeek Web"
        writer.location = "https://chat.deepseek.com/"
        writer.close = mock.Mock()

        with (
            mock.patch.object(bootstrap_smoke, "_copy_repo"),
            mock.patch.object(bootstrap_smoke, "inject_bug"),
            mock.patch.object(bootstrap_smoke.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="OK")),
            mock.patch.object(bootstrap_smoke, "connect_provider", return_value=writer),
            mock.patch.object(bootstrap_smoke, "run", return_value=mock.Mock(stop_reason="done", summary="done", turns=3)),
            mock.patch.object(bootstrap_smoke.shutil, "rmtree"),
        ):
            data = bootstrap_smoke.run_bootstrap_smoke("deepseek", port=9222, max_turns=8)

        self.assertFalse(data["ok"])
        self.assertFalse(data["initial_failure"])


if __name__ == "__main__":
    unittest.main()
