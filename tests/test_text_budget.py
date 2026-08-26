from __future__ import annotations

import unittest
from pathlib import Path

from codey.utils.text_budget import (
    OUTPUT_OMISSION_MARKER,
    clip_middle,
    prune_dependency_stack_frames,
)


class TextBudgetTests(unittest.TestCase):
    def test_clip_middle_preserves_head_and_tail_with_fixed_total_limit(self) -> None:
        text = "HEAD" + ("x" * 200) + "TAIL"

        clipped, truncated = clip_middle(text, 80)

        self.assertTrue(truncated)
        self.assertLessEqual(len(clipped), 80)
        self.assertTrue(clipped.startswith("HEAD"))
        self.assertTrue(clipped.endswith("TAIL"))
        self.assertIn(OUTPUT_OMISSION_MARKER.strip(), clipped)

    def test_clip_middle_leaves_small_output_unchanged(self) -> None:
        self.assertEqual(clip_middle("short", 80), ("short", False))

    def test_prunes_python_dependency_frame_entries(self) -> None:
        root = Path("E:/repo")
        text = (
            "Traceback (most recent call last):\n"
            '  File "E:/repo/app.py", line 3, in handle\n'
            "    service()\n"
            '  File "C:/Python/Lib/site-packages/django/core.py", line 10, in __call__\n'
            "    return handler(request)\n"
            '  File "C:/Python/Lib/site-packages/django/utils.py", line 11, in run\n'
            "    return view_func()\n"
            '  File "E:/repo/views.py", line 7, in view\n'
            "    raise ValueError('bad')\n"
            "ValueError: bad\n"
        )

        pruned = prune_dependency_stack_frames(text, root)

        self.assertIn('File "E:/repo/app.py"', pruned)
        self.assertIn("    service()", pruned)
        self.assertIn('File "E:/repo/views.py"', pruned)
        self.assertIn("ValueError: bad", pruned)
        self.assertIn("[... 2 dependency stack frames omitted ...]", pruned)
        self.assertNotIn("django/core.py", pruned)
        self.assertNotIn("return handler(request)", pruned)
        self.assertNotIn("django/utils.py", pruned)
        self.assertNotIn("return view_func()", pruned)

    def test_prunes_node_dependency_stack_frames(self) -> None:
        root = Path("E:/repo")
        text = (
            "Error: boom\n"
            "    at handler (E:/repo/src/app.js:5:1)\n"
            "    at Layer.handle (E:/repo/node_modules/express/lib/router/layer.js:95:5)\n"
            "    at next (E:/repo/node_modules/express/lib/router/route.js:144:13)\n"
            "    at route (E:/repo/src/router.js:2:1)\n"
        )

        pruned = prune_dependency_stack_frames(text, root)

        self.assertIn("at handler (E:/repo/src/app.js:5:1)", pruned)
        self.assertIn("[... 2 dependency stack frames omitted ...]", pruned)
        self.assertIn("at route (E:/repo/src/router.js:2:1)", pruned)
        self.assertNotIn("express/lib/router", pruned)

    def test_prune_keeps_project_root_named_like_dependency_segment(self) -> None:
        root = Path("E:/site-packages/repo")
        text = (
            "Traceback (most recent call last):\n"
            '  File "E:/site-packages/repo/app.py", line 1, in <module>\n'
            "    main()\n"
            "RuntimeError: local failure\n"
        )

        self.assertEqual(prune_dependency_stack_frames(text, root), text)

    def test_prune_keeps_node_project_root_named_like_dependency_segment(self) -> None:
        root = Path("E:/node_modules/repo")
        text = (
            "Error: local failure\n"
            "    at handler (E:/node_modules/repo/src/app.js:5:1)\n"
            "    at main (E:/node_modules/repo/src/main.js:2:1)\n"
        )

        self.assertEqual(prune_dependency_stack_frames(text, root), text)

    def test_prune_leaves_non_stack_logs_byte_identical(self) -> None:
        text = (
            "loading node_modules/express/index.js as fixture text\n"
            "File \"C:/Python/Lib/site-packages/django/core.py\" was mentioned in docs\n"
            "at E:/repo/node_modules/pkg/index.js while scanning fixture\n"
            "AssertionError: expected node_modules to be copied\n"
        )

        self.assertEqual(prune_dependency_stack_frames(text, Path("E:/repo")), text)


if __name__ == "__main__":
    unittest.main()
