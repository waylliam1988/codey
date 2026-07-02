from __future__ import annotations

import unittest

from codey.text_budget import OUTPUT_OMISSION_MARKER, clip_middle


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


if __name__ == "__main__":
    unittest.main()
