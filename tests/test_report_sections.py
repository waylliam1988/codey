from __future__ import annotations

import unittest

from codey.report_sections import (
    is_boundary_line,
    parse_sections,
)


class ParseSectionsBoundaryTests(unittest.TestCase):
    def test_unknown_colon_title_does_not_pollute_previous_section(self) -> None:
        body = "结论:\n- Build\n\n风险:\n- Data may stale"

        sections = parse_sections(body)

        self.assertEqual(sections, {"conclusion": "- Build"})
        self.assertNotIn("Data may stale", "\n".join(sections.values()))

    def test_unknown_markdown_heading_also_bounds_sections(self) -> None:
        body = (
            "## 结论\n"
            "- Use the API.\n"
            "\n"
            "## 备注\n"
            "Internal notes only.\n"
            "\n"
            "## 关键证据\n"
            "- [1] doc says so."
        )

        sections = parse_sections(body)

        self.assertEqual(sections["conclusion"], "- Use the API.")
        self.assertNotIn("Internal notes", "\n".join(sections.values()))
        self.assertEqual(sections["evidence"], "- [1] doc says so.")

    def test_list_items_are_never_boundaries(self) -> None:
        body = "## 反证与限制\n- 未找到强反证。\n- supplier data could overturn this."

        sections = parse_sections(body)

        self.assertIn("supplier data could overturn this", sections["counter"])
        self.assertIn("未找到强反证。", sections["counter"])

    def test_boundary_line_detection_rules(self) -> None:
        self.assertTrue(is_boundary_line("## 结论"))
        self.assertTrue(is_boundary_line("风险:"))
        self.assertTrue(is_boundary_line("Notes："))
        self.assertFalse(is_boundary_line("- risk: data may stale"))
        self.assertFalse(is_boundary_line("1. 结论 step"))
        self.assertFalse(is_boundary_line(""))
        # Long prose ending with a colon is a sentence, not a title.
        self.assertFalse(is_boundary_line(
            "The full explanation of the discount formula is the following:"
            " apply the discount before tax as documented below in detail"
        ))


if __name__ == "__main__":
    unittest.main()
