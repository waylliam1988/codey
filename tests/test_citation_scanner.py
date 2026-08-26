from __future__ import annotations

import unittest

from codey.utils.citation_scanner import (
    citation_ref_items,
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
        ])

    def test_source_id_refs_cover_bracket_and_context_only(self) -> None:
        refs = source_id_ref_items("plain s1 remains prose; [s2 p.5] and source_id=s3 are refs")

        self.assertEqual(source_id_refs("plain s1 remains prose"), set())
        self.assertEqual([(item.source_id, item.bracketed, item.page_suffix) for item in refs], [
            ("s2", True, " p.5"),
            ("s3", False, ""),
        ])


if __name__ == "__main__":
    unittest.main()
