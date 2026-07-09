from __future__ import annotations

import unittest

from codey.change_brief import new_project_change_brief, project_audit_change_brief
from codey.consensus import ConsensusAdvice


class ChangeBriefTests(unittest.TestCase):
    def test_new_project_brief_is_private_and_bounded(self) -> None:
        brief = new_project_change_brief(
            "Build a breathing app",
            "Use one HTML file and a calm animation.",
        )

        rendered = brief.render()

        self.assertIn("Private ChangeBrief", rendered)
        self.assertIn("new-project planning", rendered)
        self.assertIn("Build a breathing app", rendered)
        self.assertIn("Use one HTML file", rendered)
        self.assertIn("not persisted", rendered)

    def test_project_audit_brief_wraps_advisor_reports(self) -> None:
        brief = project_audit_change_brief(
            "Review this project",
            (ConsensusAdvice("qwen", "Qwen", "Possible bug in app.py."),),
        )

        rendered = brief.apply_to_task("Review this project")

        self.assertIn("Private ChangeBrief", rendered)
        self.assertIn("read-only project audit", rendered)
        self.assertIn("Possible bug in app.py.", rendered)
        self.assertIn("Do not modify files if the user only asked for review", rendered)


if __name__ == "__main__":
    unittest.main()
