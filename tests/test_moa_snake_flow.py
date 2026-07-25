from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import moa_snake_flow


class MoaSnakeFlowScriptTests(unittest.TestCase):
    def test_reset_project_backs_up_and_clears_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as project_td, tempfile.TemporaryDirectory() as artifacts_td:
            project = Path(project_td)
            artifacts = Path(artifacts_td)
            (project / "index.html").write_text("<canvas></canvas>", encoding="utf-8")
            nested = project / "assets"
            nested.mkdir()
            (nested / "note.txt").write_text("keep", encoding="utf-8")

            backup = moa_snake_flow.reset_project(project, artifacts)

            self.assertIsNotNone(backup)
            assert backup is not None
            self.assertTrue((backup / "index.html").is_file())
            self.assertTrue((backup / "assets" / "note.txt").is_file())
            self.assertEqual(list(project.iterdir()), [])

    def test_reset_project_preserves_project_local_codey_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as project_td:
            project = Path(project_td)
            artifacts = project / ".codey" / "smoke" / "moa-snake-flow"
            artifacts.mkdir(parents=True)
            (artifacts / "flow.log").write_text("checkpoint", encoding="utf-8")
            (project / "game.js").write_text("old", encoding="utf-8")

            backup = moa_snake_flow.reset_project(project, artifacts)

            self.assertIsNotNone(backup)
            self.assertTrue((artifacts / "flow.log").is_file())
            self.assertFalse((project / "game.js").exists())
            self.assertTrue((project / ".codey").is_dir())

    def test_verify_snake_project_checks_files_markers_and_unittest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            project = Path(td)
            (project / "index.html").write_text(
                '<canvas id="game"></canvas><script src="game.js"></script>',
                encoding="utf-8",
            )
            (project / "style.css").write_text(".score { color: white; }", encoding="utf-8")
            (project / "game.js").write_text(
                "document.addEventListener('keydown', () => {});\n"
                "const score = 0;\n"
                "const state = 'Game Over';\n"
                "function restart() {}\n",
                encoding="utf-8",
            )
            (project / "test_snake_static.py").write_text(
                "import unittest\n\n"
                "class SnakeStaticTests(unittest.TestCase):\n"
                "    def test_static(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )

            result = moa_snake_flow.verify_snake_project(project)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["missing"], [])
        self.assertTrue(all(result["markers"].values()))
        self.assertEqual(result["unittest"]["exit_code"], 0)

    def test_verify_snake_project_reports_missing_files_without_running_unittest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            result = moa_snake_flow.verify_snake_project(Path(td))

        self.assertFalse(result["ok"])
        self.assertIn("index.html", result["missing"])
        self.assertNotIn("unittest", result)

    def test_flow_recorder_writes_checkpoints_and_failure_breakpoints(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            recorder = moa_snake_flow.FlowRecorder(td)
            with mock.patch("builtins.print"):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    with recorder.stage("probe"):
                        raise RuntimeError("boom")

            checkpoint = json.loads(recorder.state_path.read_text(encoding="utf-8"))
            events = moa_snake_flow.read_jsonl(recorder.events_path)

        self.assertEqual(checkpoint["status"], "failed")
        self.assertEqual(checkpoint["stage"], "probe")
        self.assertTrue(any(event["kind"] == "stage_error" for event in events))

    def test_summarize_bottlenecks_reports_slow_sends_and_errors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            recorder = moa_snake_flow.FlowRecorder(td)
            recorder.current_stage = "advisor"
            recorder.event(
                "provider_send_done",
                provider="qwen",
                role="advisor",
                elapsed=moa_snake_flow.SLOW_SEND_SECONDS,
            )
            recorder.event(
                "provider_send_error",
                provider="stepfun",
                role="advisor",
                elapsed=1.0,
                error="stopped",
            )

            bottlenecks = moa_snake_flow.summarize_bottlenecks(recorder)

        self.assertEqual(len(bottlenecks), 2)
        self.assertEqual({item["provider"] for item in bottlenecks}, {"qwen", "stepfun"})


if __name__ == "__main__":
    unittest.main()
