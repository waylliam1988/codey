"""Lock tests for tail-truncation budget (Item 1).

Deterministic bug: ``_clip``/``compact_text`` variants did
``text[:limit].rstrip() + "\\n[truncated]"``, so the result exceeds ``limit``
by the marker length (12 chars). This also breaks the consensus combined
budget accounting (``used += len(text)``).

Red-first: these tests fail on the pre-fix code (overflow) and pass once all
callers share ``codey.utils.text_budget.clip_tail`` which reserves the marker.
"""
from __future__ import annotations

import unittest


class ClipTailBudgetTests(unittest.TestCase):
    def test_clip_tail_never_exceeds_limit(self) -> None:
        from codey.utils.text_budget import clip_tail

        for limit in (0, 1, 5, 11, 12, 13, 100, 2000, 4000):
            with self.subTest(limit=limit):
                result = clip_tail("x" * 5000, limit)
                self.assertLessEqual(len(result), limit)

    def test_clip_tail_reserves_marker_and_handles_small_limits(self) -> None:
        from codey.utils.text_budget import TRUNCATION_MARKER, clip_tail

        marker = TRUNCATION_MARKER
        self.assertEqual(marker, "\n[truncated]")
        # limit == 0 -> empty
        self.assertEqual(clip_tail("x" * 10, 0), "")
        # limit smaller than marker -> marker prefix, still bounded
        for limit in (1, 5, len(marker) - 1):
            with self.subTest(limit=limit):
                result = clip_tail("x" * 100, limit)
                self.assertEqual(result, marker[:limit])
                self.assertLessEqual(len(result), limit)
        # limit exactly marker length -> full marker
        self.assertEqual(clip_tail("x" * 100, len(marker)), marker)

    def test_clip_tail_exact_length_passthrough_and_normalization(self) -> None:
        from codey.utils.text_budget import clip_tail

        self.assertEqual(clip_tail("abc", 3), "abc")
        self.assertEqual(clip_tail("abc", 10), "abc")
        self.assertEqual(clip_tail("a\r\nb\rc", 100), "a\nb\nc")
        self.assertEqual(clip_tail(None, 10), "")
        self.assertEqual(clip_tail("  hi  ", 10), "hi")

    def test_handoff_compact_text_respects_limit(self) -> None:
        from codey.agents.handoff import compact_text

        for limit in (0, 5, 100, 2000):
            with self.subTest(limit=limit):
                self.assertLessEqual(len(compact_text("x" * 5000, limit)), limit)

    def test_change_brief_render_respects_max_brief_chars(self) -> None:
        from codey.workspace.change_brief import MAX_BRIEF_CHARS, ChangeBrief

        brief = ChangeBrief(
            source="test",
            user_intent="x" * 9000,
            observed_facts=tuple("y" * 3000 for _ in range(5)),
            planned_files=tuple("z" * 3000 for _ in range(5)),
        )
        self.assertLessEqual(len(brief.render()), MAX_BRIEF_CHARS)

    def test_evidence_pack_render_respects_max_chars(self) -> None:
        from codey.research.advisors import (
            MAX_EVIDENCE_PACK_CHARS,
            EvidenceNote,
            EvidencePack,
        )

        notes = tuple(
            EvidenceNote(
                id=f"n{i}",
                type="fact",
                title="t" * 200,
                body="b" * 5000,
                sources=("https://example.com/s",),
            )
            for i in range(12)
        )
        pack = EvidencePack(question="q" * 9000, draft="d" * 15000, notes=notes)
        self.assertLessEqual(len(pack.render()), MAX_EVIDENCE_PACK_CHARS)

    def test_review_summary_respects_field_budget(self) -> None:
        from codey.reviews.core import MAX_FIELD_CHARS, parse_review_response

        payload = (
            '{"verdict":"approved","summary":"' + "s" * 5000 + '","findings":[]}'
        )
        result = parse_review_response(payload)
        self.assertLessEqual(len(result.summary), MAX_FIELD_CHARS)

    def test_consensus_combined_advice_budget(self) -> None:
        from codey.agents.consensus import (
            MAX_COMBINED_ADVICE_CHARS,
            ConsensusAdvice,
            render_aggregator_prompt,
        )

        advices = tuple(
            ConsensusAdvice(f"a{i}", f"A{i}", "x" * 5000) for i in range(3)
        )
        prompt = render_aggregator_prompt(task="t", advices=advices)
        advice_section = prompt.split("Private advisor notes:", 1)[1]
        # advice_blocks are "Advisor N:\n<text>" joined by blank lines
        blocks = [b for b in advice_section.strip().split("\n\n") if b.startswith("Advisor")]
        bodies = [b.split("\n", 1)[1] if "\n" in b else "" for b in blocks]
        total = sum(len(b) for b in bodies)
        self.assertLessEqual(total, MAX_COMBINED_ADVICE_CHARS)
        for body in bodies:
            self.assertLessEqual(len(body), 4000)


if __name__ == "__main__":
    unittest.main()
