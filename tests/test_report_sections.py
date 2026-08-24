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

        self.assertEqual(sections["conclusion"], "- Build")
        # 风险 is an aliased section now: its content is captured under the
        # risk key instead of leaking into conclusions or being dropped.
        self.assertEqual(sections["risk"], "- Data may stale")

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
        # 备注 maps to the notes alias; either way it must not leak into
        # the conclusion or evidence sections.
        self.assertNotIn("Internal notes", sections["conclusion"])
        self.assertEqual(sections["evidence"], "- [1] doc says so.")

    def test_list_items_are_never_boundaries(self) -> None:
        body = "## 反证与限制\n- 未找到强反证。\n- supplier data could overturn this."

        sections = parse_sections(body)

        self.assertIn("supplier data could overturn this", sections["counter"])
        self.assertIn("未找到强反证。", sections["counter"])

    def test_boundary_line_detection_rules(self) -> None:
        self.assertTrue(is_boundary_line("## 结论"))
        self.assertTrue(is_boundary_line("风险:"))
        self.assertTrue(is_boundary_line("备注："))
        self.assertFalse(is_boundary_line("- risk: data may stale"))
        self.assertFalse(is_boundary_line(""))
        # A short lead-in colon line introduces the list below it; it must
        # not cut the section it belongs to.
        self.assertFalse(is_boundary_line("具体如下："))

    def test_documented_bare_numbered_headings_are_boundaries(self) -> None:
        numbered = parse_sections(
            "1. Conclusion\n"
            "- Build the tracker.\n"
            "\n"
            "2. Key Evidence\n"
            "- [1] Source supports.\n"
            "\n"
            "3. Sources\n"
            "[1] Doc - https://example.com"
        )
        self.assertEqual(numbered["conclusion"], "- Build the tracker.")
        self.assertEqual(numbered["evidence"], "- [1] Source supports.")
        self.assertEqual(numbered["sources"], "[1] Doc - https://example.com")

        chinese_numbered = parse_sections(
            "一、结论\n"
            "- Build。\n"
            "\n"
            "二、关键证据\n"
            "- [1] 支持。\n"
            "\n"
            "三、来源\n"
            "[1] 文档 - https://example.com"
        )
        self.assertIn("Build", chinese_numbered["conclusion"])
        self.assertEqual(chinese_numbered["evidence"], "- [1] 支持。")
        self.assertIn("https://example.com", chinese_numbered["sources"])

    def test_references_alias_is_recognized(self) -> None:
        sections = parse_sections("## 参考文献\n[1] Example - https://example.com")

        self.assertEqual(sections["sources"], "[1] Example - https://example.com")

    def test_lead_in_colon_line_does_not_cut_its_section(self) -> None:
        sections = parse_sections("## 结论\n具体如下：\n- Build\n\n## 来源\n[1] Example")

        self.assertIn("具体如下：\n- Build", sections["conclusion"])


if __name__ == "__main__":
    unittest.main()
