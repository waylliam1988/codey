"""Red-first: retrieval budget covers the full rendered block."""
from __future__ import annotations

import unittest

from codey.ghost.observation_index import (
    render_retrieved_block,
    retrieve_relevant_observations,
)


class ObservationBudgetContractTests(unittest.TestCase):
    def test_rendered_block_never_exceeds_budget(self) -> None:
        rows = [{
            "run_id": "r1",
            "user_text": "以后回答短一点" + "很长" * 2000,
            "assistant_text": "好的" * 2000,
            "mode": "chat",
            "ts": "2026-09-26T00:00:00Z",
        }]
        picked = retrieve_relevant_observations(rows, "以后回答短一点", budget_chars=1800)
        rendered = render_retrieved_block(picked, 1800)
        self.assertLessEqual(len(rendered), 1800)

    def test_tiny_budget_returns_empty_or_bounded(self) -> None:
        rows = [{
            "run_id": "r1",
            "user_text": "hi",
            "assistant_text": "hello",
            "mode": "chat",
            "ts": "t",
        }]
        rendered = render_retrieved_block(rows, 50)
        self.assertLessEqual(len(rendered), 50)

    def test_retrieval_selection_fits_budget(self) -> None:
        rows = [
            {"run_id": f"r{i}",
             "user_text": f"以后回答短一点第{i}条" + "内容" * 400,
             "assistant_text": "好的" + "补充" * 400,
             "mode": "chat", "ts": "2026-09-26T00:00:00Z"}
            for i in range(5)
        ]
        picked = retrieve_relevant_observations(rows, "以后回答短一点", budget_chars=1800)
        rendered = render_retrieved_block(picked, 1800)
        self.assertLessEqual(len(rendered), 1800)


if __name__ == "__main__":
    unittest.main()
