"""Hybrid display must project the kernel verdict, not re-judge quality.

Locks:
- ``_enrich_hybrid_outcome`` never adds ``report_quality``/``report_warnings``.
  Strict quality belongs in ``completion_gate`` before ``done``.
- ``stop_reason`` is the kernel's verdict; post-hoc display never flips it.
- No misleading ``Project review trigger`` comment/block.
"""
from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace


class HybridOutcomeProjectionTests(unittest.TestCase):
    def test_enrich_never_adds_report_quality(self) -> None:
        from codey.operations import task_entry as te

        source = inspect.getsource(te._enrich_hybrid_outcome)
        self.assertNotIn("report_quality", source)
        self.assertNotIn("report_warnings", source)
        self.assertNotIn("review_report_quality", source)

    def test_enrich_has_no_misleading_review_trigger_comment(self) -> None:
        from codey.operations import task_entry as te

        source = inspect.getsource(te._enrich_hybrid_outcome)
        self.assertNotIn("Project review trigger", source)

    def test_enrich_preserves_stop_reason_and_projects_facts(self) -> None:
        from codey.operations import task_entry as te
        from codey.operations.result import ModeOutcome

        session = SimpleNamespace(
            edited_files={"a.py": 1},
            verifications=[{"command": "python -m unittest discover", "revision": 1}],
            search_results={},
        )
        outcome = ModeOutcome({
            "type": "task_done", "run_id": "r", "session_id": "s",
            "summary": "summary text", "stop_reason": "done",
            "turns": 1, "max_turns": 8, "provider": "p", "mode": "hybrid",
            "receipt": {"display": {"summary": "summary text"}},
        })
        work = SimpleNamespace(evidence=SimpleNamespace(render_for_review=lambda: "ev"))
        enriched = te._enrich_hybrid_outcome(
            SimpleNamespace(), work, outcome, session=session, research_tools=None,
        )
        event = dict(enriched.event or {})
        self.assertEqual(event.get("stop_reason"), "done")
        display = ((event.get("receipt") or {}).get("display") or {})
        self.assertNotIn("report_quality", display)
        # Projection of already-decided facts is allowed.
        self.assertIn("changed_files", display)

    def test_enrich_projects_hybrid_research_facts_with_note_ids(self) -> None:
        from codey.operations import task_entry as te
        from codey.operations.result import ModeOutcome

        class Ledger:
            searches = [SimpleNamespace(query="alpha")]

            def search_results_payload(self):
                return [{"url": "https://example.test/a", "title": "A"}]

            def opened_sources_payload(self):
                return [{"url": "https://example.test/a"}]

            def evidence_payload(self):
                return [{"evidence_id": "ev-1"}]

            def coverage_payload(self):
                return {"matched": 1, "total": 1}

        tools = SimpleNamespace(
            created_ids=["note-1"], updated_ids=["note-2"],
            sources_read={"https://example.test/a"}, ledger=Ledger(),
            links_created=1,
        )
        outcome = ModeOutcome({
            "type": "task_done", "run_id": "r", "session_id": "s",
            "summary": "summary text", "stop_reason": "done", "turns": 1,
            "max_turns": 8, "provider": "p", "mode": "hybrid",
            "receipt": {"display": {"summary": "summary text"}},
        })
        enriched = te._enrich_hybrid_outcome(
            SimpleNamespace(request=SimpleNamespace(task="alpha")),
            SimpleNamespace(evidence=SimpleNamespace(render_for_review=lambda: "ev")),
            outcome, session=SimpleNamespace(), research_tools=tools,
        )
        research = enriched.event["research"]
        self.assertEqual(research["notes_created"], ["note-1"])
        self.assertEqual(research["notes_updated"], ["note-2"])
        self.assertEqual(research["queries"], ["alpha"])
        self.assertEqual(research["source_urls"], ["https://example.test/a"])

    def test_enrich_preserves_mode_outcome_metadata_and_explicit_synthesis_id(self) -> None:
        from codey.operations import task_entry as te
        from codey.operations.result import ModeOutcome

        outcome = ModeOutcome(
            {"type": "task_done", "summary": "done", "receipt": {"display": {}}},
            display=({"answer": "kept"},), research_result={"kept": True},
        )
        tools = SimpleNamespace(
            ledger=SimpleNamespace(
                searches=[], search_results_payload=lambda: [],
                opened_sources_payload=lambda: [], evidence_payload=lambda: [],
                coverage_payload=lambda: {},
            ),
            created_ids=["20261014T175521-title-f375a657"], updated_ids=[],
            synthesis_id="20261014T175521-title-f375a657", sources_read=set(), links_created=0,
        )
        enriched = te._enrich_hybrid_outcome(
            SimpleNamespace(request=SimpleNamespace(task="q")),
            SimpleNamespace(evidence=None), outcome, research_tools=tools,
        )
        self.assertEqual(enriched.display, ({"answer": "kept"},))
        self.assertEqual(enriched.research_result, {"kept": True})
        self.assertEqual(enriched.event["research"]["synthesis_id"], tools.synthesis_id)


if __name__ == "__main__":
    unittest.main()
