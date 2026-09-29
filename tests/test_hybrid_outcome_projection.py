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


if __name__ == "__main__":
    unittest.main()
