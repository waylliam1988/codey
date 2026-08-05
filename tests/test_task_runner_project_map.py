from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from codey import project_task_context
from codey.project_map import MAX_PROJECT_MAP_CHARS


class TaskRunnerProjectMapTests(unittest.TestCase):
    def test_safe_project_map_passes_task_to_renderer(self) -> None:
        with mock.patch.object(project_task_context, "render_project_map", return_value="map") as render:
            rendered = project_task_context.safe_project_map(
                Path("project"),
                "- successful check: python -m unittest",
                "change json codec",
            )

        self.assertEqual(rendered, "map")
        render.assert_called_once_with(
            Path("project"),
            "- successful check: python -m unittest",
            task="change json codec",
            ignored_paths=(),
            max_chars=MAX_PROJECT_MAP_CHARS,
        )


if __name__ == "__main__":
    unittest.main()
