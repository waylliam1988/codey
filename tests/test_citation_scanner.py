from __future__ import annotations

import unittest

from codey.utils.citation_scanner import (
    citation_ref_items,
    normalize_citation_groups,
    source_id_ref_items,
    source_id_refs,
)


class CitationScannerTests(unittest.TestCase):
    def test_numeric_citations_keep_page_ranges(self) -> None:
        refs = citation_ref_items("Alpha [2 p.3-4], ordinal [2nd], beta [10 pages 7].")

        self.assertEqual([(item.number, item.pages) for item in refs], [
            (2, (3, 4)),
            (10, (7,)),
        ])

    def test_numeric_citations_allow_adjacent_refs_without_array_indexes(self) -> None:
        refs = citation_ref_items("结论 [1][2]; array[0] per [3]; comma [1, 2].")

        self.assertEqual([(item.number, item.pages) for item in refs], [
            (1, ()),
            (2, ()),
            (3, ()),
            (1, ()),
            (2, ()),
        ])

    def test_explicit_group_normalization_does_not_guess_ranges_or_array_indices(self) -> None:
        self.assertEqual(normalize_citation_groups("Sources [2, 7，10]."), "Sources [2][7][10].")
        untouched = "array[1,2]; ![1,2](image); [1,2](url); [[1,2]]; [1-3]; [1 p.2,3]"
        self.assertEqual(normalize_citation_groups(untouched), untouched)

    def test_source_id_refs_cover_bracket_and_context_only(self) -> None:
        refs = source_id_ref_items("plain s1 remains prose; [s2 p.5] and source_id=s3 are refs")

        self.assertEqual(source_id_refs("plain s1 remains prose"), set())
        self.assertEqual([(item.source_id, item.bracketed, item.page_suffix) for item in refs], [
            ("s2", True, " p.5"),
            ("s3", False, ""),
        ])

    def test_source_id_refs_cover_common_report_renderings(self) -> None:
        text = (
            "结论（来源s2、s3）；关键证据（来源 s4 p.5）；限制 (s5)。\n"
            "| s6 (paper) | quality |\n"
            "- s7: source title"
        )

        refs = source_id_ref_items(text)

        self.assertEqual(
            [(item.source_id, item.bracketed, item.page_suffix) for item in refs],
            [
                ("s2", True, ""),
                ("s3", True, ""),
                ("s4", True, " p.5"),
                ("s5", True, ""),
                ("s6", False, ""),
                ("s7", False, ""),
            ],
        )

    def test_source_id_refs_do_not_treat_plain_ids_as_citations(self) -> None:
        text = "plain s1 remains prose; a source was s3; source s12abc is a token"

        self.assertEqual(source_id_refs(text), set())


if __name__ == "__main__":
    unittest.main()
