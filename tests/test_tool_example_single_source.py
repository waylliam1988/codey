"""Lock tests for Research tool_example single source (Item 2).

Deterministic bug: ``tool_example()`` hand-maintained a legacy copy while
``TOOL_CONTRACTS[name].example`` is the static contract. They drifted, e.g.
``knowledge_link.dst`` allows "note ID or exact title" in the contract but the
dynamic example only says "note ID". The model-visible repair prompts therefore
teach a narrower shape than the validator accepts.

Red-first: equality assertions fail on pre-fix code for open_url,
knowledge_write, knowledge_link, and done.
"""
from __future__ import annotations

import unittest


class ToolExampleSingleSourceTests(unittest.TestCase):
    def test_tool_example_matches_contract_for_all_tools(self) -> None:
        from codey.research.tool_contract import TOOL_CONTRACTS, tool_example

        for name, contract in TOOL_CONTRACTS.items():
            with self.subTest(tool=name):
                self.assertEqual(tool_example(name), contract.example)

    def test_knowledge_link_teaches_exact_title(self) -> None:
        from codey.research.tool_contract import tool_example

        example = tool_example("knowledge_link")
        self.assertIn("exact title", example)

    def test_unknown_tool_falls_back_to_web_search(self) -> None:
        from codey.research.tool_contract import TOOL_CONTRACTS, tool_example

        self.assertEqual(tool_example("no_such_tool"), TOOL_CONTRACTS["web_search"].example)
        self.assertEqual(tool_example(""), TOOL_CONTRACTS["web_search"].example)

    def test_controller_dynamic_ids_still_use_state(self) -> None:
        from tests.support.research_controller import ResearchControlState, controller_tool_example

        state = ResearchControlState(
            allowed_tools=("open_result", "reopen_source", "open_hit", "source_search"),
            result_urls={"r7": "https://example.com/r"},
            source_urls={"s3": "https://example.com/s"},
            hit_targets={"h9": "https://example.com/s"},
        )
        # open_result/reopen_source/open_hit must embed live IDs, not contracts
        self.assertIn("r7", controller_tool_example("open_result", state))
        self.assertIn("s3", controller_tool_example("reopen_source", state))
        self.assertIn("h9", controller_tool_example("open_hit", state))
        # source_search with an opened source uses the live source_id shape
        self.assertIn("s3", controller_tool_example("source_search", state))

    def test_controller_generic_tools_match_contract(self) -> None:
        from codey.research.tool_contract import TOOL_CONTRACTS
        from tests.support.research_controller import ResearchControlState, controller_tool_example

        state = ResearchControlState(allowed_tools=("knowledge_link", "done"))
        self.assertEqual(
            controller_tool_example("knowledge_link", state),
            TOOL_CONTRACTS["knowledge_link"].example,
        )
        self.assertEqual(
            controller_tool_example("done", state),
            TOOL_CONTRACTS["done"].example,
        )


if __name__ == "__main__":
    unittest.main()
