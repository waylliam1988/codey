from __future__ import annotations

import unittest

from codey.runtime import cancellation
from codey.workspace.context_source import (
    FAILURE_POLICY_RAISE,
    ContextSource,
    render_context_source,
    render_context_sources,
)


class ContextSourceTests(unittest.TestCase):
    def test_empty_source_omits_heading_and_keeps_order(self) -> None:
        rendered = render_context_sources((
            ContextSource(
                key="empty",
                loader=lambda: "  ",
                budget=100,
                freshness="run_start",
                why_included="empty test source",
                heading="Empty:",
            ),
            ContextSource(
                key="first",
                loader=lambda: "alpha",
                budget=100,
                freshness="run_start",
                why_included="first test source",
                heading="First:",
            ),
            ContextSource(
                key="second",
                loader=lambda: "beta",
                budget=100,
                freshness="run_start",
                why_included="second test source",
            ),
        ))

        self.assertEqual(rendered, "First:\nalpha\n\nbeta")
        self.assertNotIn("Empty:", rendered)

    def test_budget_clips_rendered_block_and_preserves_metadata(self) -> None:
        source = ContextSource(
            key="project_map",
            loader=lambda: "A" * 120 + "\nTAIL",
            budget=80,
            freshness="run_start",
            why_included="bounded local project map",
            heading="Project Map:",
        )

        rendered = render_context_source(source)

        self.assertIsNotNone(rendered)
        assert rendered is not None
        self.assertEqual(rendered.key, "project_map")
        self.assertEqual(rendered.budget, 80)
        self.assertEqual(rendered.freshness, "run_start")
        self.assertEqual(rendered.why_included, "bounded local project map")
        self.assertTrue(rendered.truncated)
        self.assertLessEqual(len(rendered.text), 80)
        self.assertIn("middle of output omitted", rendered.text)
        self.assertIn("TAIL", rendered.text)

    def test_loader_exception_omits_source_by_default(self) -> None:
        def broken() -> str:
            raise OSError("cannot list")

        rendered = render_context_sources((
            ContextSource(
                key="initial_listing",
                loader=broken,
                budget=100,
                freshness="run_start",
                why_included="current top-level project listing",
                heading="Initial listing:",
            ),
            ContextSource(
                key="task",
                loader=lambda: "still here",
                budget=100,
                freshness="run_start",
                why_included="next source",
            ),
        ))

        self.assertEqual(rendered, "still here")
        self.assertNotIn("Initial listing:", rendered)

    def test_raise_failure_policy_reraises_loader_exception(self) -> None:
        def broken() -> str:
            raise ValueError("bad source")

        with self.assertRaises(ValueError):
            render_context_source(ContextSource(
                key="debug",
                loader=broken,
                budget=100,
                freshness="run_start",
                why_included="debug source",
                failure_policy=FAILURE_POLICY_RAISE,
            ))

    def test_cancel_and_deadline_are_not_swallowed(self) -> None:
        cases = (
            cancellation.TaskCancelled("stop"),
            cancellation.DeadlineExceeded("timeout"),
        )

        for exc in cases:
            with self.subTest(exc=type(exc).__name__):
                def stopped() -> str:
                    raise exc

                with self.assertRaises(type(exc)):
                    render_context_source(ContextSource(
                        key="initial_listing",
                        loader=stopped,
                        budget=100,
                        freshness="run_start",
                        why_included="current top-level project listing",
                    ))

    def test_invalid_failure_policy_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            render_context_source(ContextSource(
                key="bad_policy",
                loader=lambda: "content",
                budget=100,
                freshness="run_start",
                why_included="bad policy test",
                failure_policy="ignore",
            ))


if __name__ == "__main__":
    unittest.main()