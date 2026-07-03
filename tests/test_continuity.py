from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from codey import changes, server


class LocalContinuityTests(unittest.TestCase):
    def test_all_local_state_survives_abrupt_process_exit(self) -> None:
        script = (
            "import os,sys; from pathlib import Path; from codey import server; "
            "from codey.handoff import ConversationSnapshot; "
            "root=Path(sys.argv[1]); state=server.State(sys.argv[2]); "
            "state.project_facts.record_success(root,'.','python -m unittest'); "
            "context=state.conversation_for('chat-1'); "
            "context.begin_window('deepseek','project',str(root.resolve())); "
            "context.update_snapshot(ConversationSnapshot(mode='project',goal='ORANGE-417',project=str(root.resolve()),provider_id='deepseek')); "
            "tracker=state.change_tracker_for(root,persistent=True); "
            "tracker.capture_before('app.py'); "
            "(root/'app.py').write_text('new\\n',encoding='utf-8'); "
            "tracker.capture_after('app.py'); os._exit(0)"
        )
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as state_td:
            root = Path(td)
            path = root / "app.py"
            path.write_text("old\n", encoding="utf-8")

            proc = subprocess.run(
                [sys.executable, "-B", "-c", script, str(root), state_td],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                check=False,
            )
            restarted = server.State(state_td)
            facts = restarted.project_facts.render(root)
            conversation = restarted.conversation_for("chat-1")
            tracker = restarted.change_tracker_for(root, persistent=True)
            change_data = changes.collect_changes(root, tracker)
            restored = tracker.restore()

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("python -m unittest", facts)
            self.assertEqual(conversation.snapshot.goal, "ORANGE-417")
            self.assertEqual(change_data["changed_count"], 1)
            self.assertTrue(restored.ok, restored)
            self.assertEqual(path.read_text(encoding="utf-8"), "old\n")


if __name__ == "__main__":
    unittest.main()
