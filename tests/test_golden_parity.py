from __future__ import annotations

import unittest
from pathlib import Path

from codey.protocols.json_codec import JsonToolCodec
from codey.research.controller import ResearchController, render_control_block
from codey.research.protocols import JsonToolCodec as ResearchCodec

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "golden"


class GoldenParityTests(unittest.TestCase):
    def _assert_fixture(self, name: str, actual: str) -> None:
        path = FIXTURE_ROOT / name
        expected = path.read_text(encoding="utf-8")
        self.assertEqual(actual, expected, f"golden mismatch for {name}: expected {len(expected)} chars, got {len(actual)}")

    def test_coding_writer_and_readonly_golden(self) -> None:
        writer = JsonToolCodec(permission_profile="coding_writer")
        readonly = JsonToolCodec(permission_profile="planning_readonly")
        self._assert_fixture("coding_coding_writer_system_prompt.txt", writer.system_prompt())
        self._assert_fixture("coding_planning_readonly_system_prompt.txt", readonly.system_prompt())

    def test_research_system_prompts_golden(self) -> None:
        full = ResearchCodec(include_source_search=True)
        thin = ResearchCodec(include_source_search=False)
        self._assert_fixture("research_full_system_prompt.txt", full.system_prompt())
        self._assert_fixture("research_thin_system_prompt.txt", thin.system_prompt())

    def test_controller_system_prompts_golden(self) -> None:
        from codey.research.controller import controller_system_prompt

        full = controller_system_prompt(include_source_search=True)
        thin = controller_system_prompt(include_source_search=False)
        self._assert_fixture("controller_full_system_prompt.txt", full)
        self._assert_fixture("controller_thin_system_prompt.txt", thin)

    def test_controller_control_block_golden(self) -> None:
        # minimal ledger to produce a deterministic block
        import tempfile
        from pathlib import Path
        from codey.knowledge.store import KnowledgeStore
        from codey.knowledge.changes import KnowledgeChanges
        from codey.research.tools import ResearchTools

        class FakeSearch:
            def search(self, q, limit=8):
                return []
            def fetch(self, url):
                return {}

        # Use a temporary store but avoid cleanup permission issues by using a fixed temp dir
        import shutil
        td = tempfile.mkdtemp()
        try:
            store = KnowledgeStore(Path(td))
            changes = KnowledgeChanges(root=store.root)
            tools = ResearchTools(search=FakeSearch(), store=store, changes=changes, diagnostics=None, session_id="s", project="p")
            ctrl = ResearchController(include_source_search=True)
            state = ctrl.build_state(tools, turn=1, max_turns=14)
            block = render_control_block(state)
            self._assert_fixture("controller_control_block_turn1.txt", block)
            store.close()
        finally:
            try:
                shutil.rmtree(td, ignore_errors=True)
            except Exception:
                pass

    def test_research_repair_prompt_golden(self) -> None:
        from codey.research.tool_contract import PROTOCOL_DISALLOWED_TOOL
        from codey.research.controller import ResearchController, ResearchControlState
        from codey.research.protocols import JsonToolCodec as ResearchCodec
        from codey.research.runner import render_research_repair_prompt
        from codey.runtime.models import ToolPlan

        state = ResearchControlState(
            allowed_tools=("knowledge_search", "knowledge_read", "web_search", "open_result", "done"),
            evidence_count=0,
            note_count=0,
            done_escape=True,
            result_lines=("r1: Helium article - https://example.com/helium - Helium supply.",),
            result_urls={"r1": "https://example.com/helium"},
        )
        plan = ToolPlan(
            calls=[],
            control=None,
            protocol_error="knowledge_write is not allowed by the current Research controller state; allowed tools: knowledge_search, knowledge_read, web_search, open_result, done",
            protocol_error_kind=PROTOCOL_DISALLOWED_TOOL,
            protocol_tool_name="knowledge_write",
        )
        codec = ResearchCodec(include_source_search=False)
        repair_body = render_research_repair_prompt(codec, plan, state)
        ctrl = ResearchController(include_source_search=False)
        actual = ctrl.append_block(repair_body, state)
        self._assert_fixture("research_repair_synthesis.txt", actual)


if __name__ == "__main__":
    unittest.main()
