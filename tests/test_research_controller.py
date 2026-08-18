from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from codey.models import ToolCall, ToolResult
from codey.research.controller import (
    CONTROLLER_DISPLAY_LIMIT,
    OpenTarget,
    ResearchControlState,
    ResearchController,
    controller_system_prompt,
    format_controller_results,
    render_control_block,
)
from codey.research.ledger import ResearchLedger
from codey.research.protocols import JsonToolCodec
from codey.research.source_document import SourceDocument, SourcePage
from codey.research.tool_contract import PROTOCOL_NO_JSON


def tools_for(ledger: ResearchLedger) -> SimpleNamespace:
    return SimpleNamespace(
        ledger=ledger,
        created_ids=[],
        updated_ids=[],
        grounded_ids=set(),
    )


class ResearchControllerTests(unittest.TestCase):
    def test_initial_state_exposes_only_memory_and_search_tools(self) -> None:
        controller = ResearchController()
        state = controller.build_state(tools_for(ResearchLedger()), turn=1, max_turns=8)

        self.assertEqual(state.allowed_tools, ("knowledge_search", "knowledge_read", "web_search"))
        self.assertNotIn("open_url", state.allowed_tools)
        self.assertNotIn("done", state.allowed_tools)
        self.assertIn("Allowed tools this turn: knowledge_search, knowledge_read, web_search", render_control_block(state))

    def test_controller_prompt_teaches_tags_and_relations_discipline(self) -> None:
        prompt = controller_system_prompt()

        self.assertIn("Note tags should be 2-5 short lowercase concept nouns", prompt)
        self.assertIn(
            '{"src":...,"dst":...,"kind":affects/uses/causes/part_of/enables/relates}', prompt
        )
        self.assertIn("never declare a guessed relation", prompt)
        self.assertNotIn("Codey", prompt)

    def test_knowledge_write_shape_includes_tags_and_relations(self) -> None:
        state = ResearchControlState(
            allowed_tools=("knowledge_write",),
            source_urls={"s1": "https://example.com/a"},
        )
        block = render_control_block(state)

        example = next(
            line[2:] for line in block.splitlines()
            if line.startswith('- {"tool":"knowledge_write"')
        )
        payload = json.loads(example)
        self.assertEqual(payload["args"]["sources"], ["s1"])
        self.assertEqual(
            payload["args"]["relations"], [{"src": "...", "dst": "...", "kind": "affects"}]
        )
        self.assertIn("tags", payload["args"])

    def test_control_block_exposes_distinct_open_actions(self) -> None:
        state = ResearchControlState(
            allowed_tools=("open_result", "reopen_source", "open_hit", "source_search"),
            result_urls={"r1": "https://example.com/search-result"},
            source_urls={"s1": "https://example.com/opened"},
            hit_targets={"h1": OpenTarget("https://example.com/opened", offset=1200)},
        )
        block = render_control_block(state)

        self.assertIn('{"tool":"open_result","args":{"result_id":"r1"}}', block)
        self.assertIn('{"tool":"reopen_source","args":{"source_id":"s1"', block)
        self.assertIn('{"tool":"open_hit","args":{"hit_id":"h1"}}', block)
        self.assertNotIn('{"tool":"open_url","args":{"result_id"', block)
        self.assertNotIn('{"tool":"open_url","args":{"source_id"', block)
        self.assertNotIn('{"tool":"open_url","args":{"hit_id"', block)

    def test_control_block_omits_source_search_guidance_when_disabled(self) -> None:
        state = ResearchControlState(
            allowed_tools=("knowledge_search", "knowledge_read", "web_search"),
        )
        block = render_control_block(state)

        self.assertNotIn("source_search with source_id", block)
        self.assertNotIn("open_hit", block)
        self.assertNotIn("Codey", block)

    def test_controller_result_followup_hides_runtime_open_url_action(self) -> None:
        prompt = format_controller_results([
            ToolResult(ToolCall("open_url", {"url": "https://example.com/a"}), "opened text")
        ])

        self.assertIn('[result: opened_source "https://example.com/a"]', prompt)
        self.assertIn("open_result/reopen_source/open_hit", prompt)
        self.assertNotIn("[result: open_url", prompt)
        self.assertNotIn("call open_url", prompt)
        self.assertNotIn("Codey", prompt)

    def test_search_result_ids_are_run_global_stable(self) -> None:
        ledger = ResearchLedger()
        controller = ResearchController()
        ledger.record_search("alpha", [
            {"title": "Alpha", "url": "https://example.com/a", "snippet": "first"},
            {"title": "Beta", "url": "https://example.com/b", "snippet": "second"},
        ])
        first = controller.build_state(tools_for(ledger), turn=2, max_turns=8)
        ledger.record_search("beta", [
            {"title": "Beta again", "url": "https://example.com/b", "snippet": "again"},
            {"title": "Gamma", "url": "https://example.com/c", "snippet": "third"},
        ])
        second = controller.build_state(tools_for(ledger), turn=3, max_turns=8)

        self.assertEqual(first.result_urls["r1"], "https://example.com/a")
        self.assertEqual(first.result_urls["r2"], "https://example.com/b")
        self.assertEqual(second.result_urls["r2"], "https://example.com/b")
        self.assertEqual(second.result_urls["r3"], "https://example.com/c")

    def test_control_block_displays_recent_ids_without_dropping_old_mappings(self) -> None:
        ledger = ResearchLedger()
        controller = ResearchController()
        ledger.record_search("many-a", [
            {"title": f"Result A{index}", "url": f"https://example.com/a{index}", "snippet": f"snippet {index}"}
            for index in range(1, CONTROLLER_DISPLAY_LIMIT + 1)
        ])
        ledger.record_search("many-b", [
            {"title": f"Result B{index}", "url": f"https://example.com/b{index}", "snippet": f"snippet {index}"}
            for index in range(1, CONTROLLER_DISPLAY_LIMIT + 1)
        ])
        for index in range(1, CONTROLLER_DISPLAY_LIMIT + 3):
            ledger.record_open(
                f"https://example.com/s{index}",
                f"https://example.com/s{index}",
                f"Source {index}",
                f"source text {index}",
            )
        ledger.record_source_search(
            "https://example.com/s10",
            "target",
            [
                {"offset": index * 100, "snippet": f"hit text {index}"}
                for index in range(1, CONTROLLER_DISPLAY_LIMIT + 3)
            ],
        )

        state = controller.build_state(tools_for(ledger), turn=5, max_turns=12)

        self.assertIn("r1", state.result_urls)
        self.assertFalse(any(line.split(":", 1)[0] == "r1" for line in state.result_lines))
        self.assertTrue(state.result_lines[0].startswith("r9:"))
        self.assertTrue(state.result_lines[-1].startswith("r16:"))
        self.assertIn("s1", state.source_urls)
        self.assertFalse(any(line.split(":", 1)[0] == "s1" for line in state.source_lines))
        self.assertTrue(state.source_lines[0].startswith("s3:"))
        self.assertTrue(state.source_lines[-1].startswith("s10:"))
        self.assertIn("h1", state.hit_targets)
        self.assertFalse(any(line.split(":", 1)[0] == "h1" for line in state.hit_lines))
        self.assertTrue(state.hit_lines[0].startswith("h3:"))
        self.assertTrue(state.hit_lines[-1].startswith("h10:"))

    def test_open_result_compiles_to_runtime_open_url_before_codec_contract(self) -> None:
        ledger = ResearchLedger()
        ledger.record_search("alpha", [{"title": "Alpha", "url": "https://example.com/a"}])
        controller = ResearchController()
        state = controller.build_state(tools_for(ledger), turn=2, max_turns=8)

        plan = controller.parse_plan(
            JsonToolCodec(),
            '{"tool":"open_result","args":{"result_id":"r1"}}',
            state,
        )

        self.assertFalse(plan.protocol_error)
        self.assertEqual(plan.calls, [ToolCall("open_url", {
            "url": "https://example.com/a",
            "offset": 0,
            "limit": 6000,
            "pages": "",
        })])

    def test_old_open_url_id_protocol_is_not_controller_visible(self) -> None:
        ledger = ResearchLedger()
        ledger.record_search("alpha", [{"title": "Alpha", "url": "https://example.com/a"}])
        controller = ResearchController()
        state = controller.build_state(tools_for(ledger), turn=2, max_turns=8)

        plan = controller.parse_plan(
            JsonToolCodec(),
            '{"tool":"open_url","args":{"result_id":"r1"}}',
            state,
        )

        self.assertEqual(plan.protocol_error_kind, "unknown_tool")
        self.assertIn("unknown tool: open_url", plan.protocol_error)

    def test_controller_does_not_repair_provider_specific_typographic_quotes(self) -> None:
        ledger = ResearchLedger()
        ledger.record_search("alpha", [{"title": "Alpha", "url": "https://example.com/a"}])
        controller = ResearchController()
        state = controller.build_state(tools_for(ledger), turn=2, max_turns=8)

        plan = controller.parse_plan(
            JsonToolCodec(),
            "{“tool”:“open_result”,“args”:{“result_id”:“r1”}}",
            state,
        )

        self.assertEqual(plan.protocol_error_kind, PROTOCOL_NO_JSON)
        self.assertFalse(plan.calls)

    def test_stable_ids_override_conflicting_handwritten_urls(self) -> None:
        ledger = ResearchLedger()
        ledger.record_search("alpha", [{"title": "Alpha", "url": "https://example.com/a"}])
        ledger.record_open("https://example.com/a", "https://example.com/a", "Alpha", "Alpha evidence text")
        controller = ResearchController()
        state = controller.build_state(tools_for(ledger), turn=3, max_turns=8)

        result_plan = controller.parse_plan(
            JsonToolCodec(),
            '{"tool":"open_result","args":{"result_id":"r1","url":"https://wrong.example/b"}}',
            state,
        )
        source_plan = controller.parse_plan(
            JsonToolCodec(),
            '{"tool":"reopen_source","args":{"source_id":"s1","url":"https://wrong.example/b"}}',
            state,
        )
        source_search_plan = controller.parse_plan(
            JsonToolCodec(),
            '{"tool":"source_search","args":{"source_id":"s1","url":"https://wrong.example/b","query":"evidence"}}',
            state,
        )

        self.assertEqual(result_plan.calls[0].args["url"], "https://example.com/a")
        self.assertEqual(source_plan.calls[0].args["url"], "https://example.com/a")
        self.assertEqual(source_search_plan.calls[0].args["url"], "https://example.com/a")

    def test_source_id_rewrites_source_search_and_knowledge_write(self) -> None:
        ledger = ResearchLedger()
        ledger.record_open("https://example.com/a", "https://example.com/a", "Alpha", "Alpha evidence text")
        controller = ResearchController()
        state = controller.build_state(tools_for(ledger), turn=3, max_turns=8)

        search_plan = controller.parse_plan(
            JsonToolCodec(),
            '{"tool":"source_search","args":{"source_id":"s1","query":"evidence"}}',
            state,
        )
        write_plan = controller.parse_plan(
            JsonToolCodec(),
            json.dumps({
                "tool": "knowledge_write",
                "args": {
                    "type": "fact",
                    "title": "Alpha",
                    "body": "Alpha evidence text",
                    "sources": ["s1"],
                    "evidence": {
                        "claim": "Alpha has evidence.",
                        "source_url": "s1",
                        "excerpt": "Alpha evidence text",
                    },
                },
            }),
            state,
        )

        self.assertEqual(search_plan.calls[0].args["url"], "https://example.com/a")
        self.assertEqual(write_plan.calls[0].args["sources"], ["https://example.com/a"])
        self.assertEqual(write_plan.calls[0].args["evidence"][0]["source_url"], "https://example.com/a")

    def test_hit_id_rewrites_to_pdf_page_open_target(self) -> None:
        ledger = ResearchLedger()
        ledger.record_open_document(SourceDocument(
            requested_url="https://example.com/report.pdf",
            final_url="https://example.com/report.pdf",
            title="Report",
            content_kind="pdf",
            mime_type="application/pdf",
            text="[page 1]\nintro",
            page_count=9,
            pages_read=(1,),
            page_texts=(SourcePage(number=1, text="intro"),),
        ))
        ledger.record_source_search(
            "https://example.com/report.pdf",
            "target",
            [{"page": 9, "offset": 20, "snippet": "target phrase"}],
        )
        controller = ResearchController()
        state = controller.build_state(tools_for(ledger), turn=4, max_turns=8)

        plan = controller.parse_plan(
            JsonToolCodec(),
            '{"tool":"open_hit","args":{"hit_id":"h1"}}',
            state,
        )

        self.assertEqual(plan.calls[0].args["url"], "https://example.com/report.pdf")
        self.assertEqual(plan.calls[0].args["pages"], "9")
        self.assertEqual(plan.calls[0].args["offset"], 0)

    def test_hit_id_rewrites_to_html_offset_open_target(self) -> None:
        ledger = ResearchLedger()
        ledger.record_open("https://example.com/a", "https://example.com/a", "Alpha", "Alpha evidence text")
        ledger.record_source_search(
            "https://example.com/a",
            "target",
            [{"offset": 18400, "snippet": "target phrase"}],
        )
        controller = ResearchController()
        state = controller.build_state(tools_for(ledger), turn=4, max_turns=8)

        plan = controller.parse_plan(
            JsonToolCodec(),
            '{"tool":"open_hit","args":{"hit_id":"h1"}}',
            state,
        )

        self.assertEqual(plan.calls[0].args["url"], "https://example.com/a")
        self.assertEqual(plan.calls[0].args["offset"], 18400)
        self.assertEqual(plan.calls[0].args["limit"], 6000)
        self.assertEqual(plan.calls[0].args["pages"], "")

    def test_unknown_stable_ids_are_invalid_args(self) -> None:
        ledger = ResearchLedger()
        ledger.record_search("alpha", [{"title": "Alpha", "url": "https://example.com/a"}])
        ledger.record_open("https://example.com/a", "https://example.com/a", "Alpha", "Alpha evidence text")
        ledger.record_source_search("https://example.com/a", "target", [{"offset": 100, "snippet": "target"}])
        controller = ResearchController()
        state = controller.build_state(tools_for(ledger), turn=4, max_turns=8)

        result_plan = controller.parse_plan(JsonToolCodec(), '{"tool":"open_result","args":{"result_id":"r9"}}', state)
        source_plan = controller.parse_plan(JsonToolCodec(), '{"tool":"reopen_source","args":{"source_id":"s9"}}', state)
        hit_plan = controller.parse_plan(JsonToolCodec(), '{"tool":"open_hit","args":{"hit_id":"h9"}}', state)

        self.assertEqual(result_plan.protocol_error_kind, "invalid_args")
        self.assertIn("unknown result_id: r9", result_plan.protocol_error)
        self.assertEqual(source_plan.protocol_error_kind, "invalid_args")
        self.assertIn("unknown source_id: s9", source_plan.protocol_error)
        self.assertEqual(hit_plan.protocol_error_kind, "invalid_args")
        self.assertIn("unknown hit_id: h9", hit_plan.protocol_error)

    def test_done_allowed_after_evidence_or_near_limit_escape(self) -> None:
        ledger = ResearchLedger()
        controller = ResearchController()
        ledger.record_search("alpha", [{"title": "Alpha", "url": "https://example.com/a"}])
        early = controller.build_state(tools_for(ledger), turn=2, max_turns=8)
        late = controller.build_state(tools_for(ledger), turn=7, max_turns=8)
        ledger = ResearchLedger()
        ledger.record_open("https://example.com/a", "https://example.com/a", "Alpha", "Alpha evidence text")
        evidence = ledger.prepare_evidence_items(
            [{
                "claim": "Alpha has evidence.",
                "source_url": "https://example.com/a",
                "excerpt": "Alpha evidence text",
            }],
            fallback_sources=["https://example.com/a"],
            fallback_claim="Alpha has evidence.",
            fallback_body="Alpha evidence text",
            note_type="fact",
        )
        assert not evidence.error
        ledger.add_evidence_items(list(evidence.items), note_id="fact-1")
        with_evidence = controller.build_state(tools_for(ledger), turn=3, max_turns=8)

        self.assertNotIn("done", early.allowed_tools)
        self.assertIn("done", late.allowed_tools)
        self.assertTrue(late.done_escape)
        self.assertIn("done", with_evidence.allowed_tools)

    def test_disallowed_tool_is_typed_protocol_error(self) -> None:
        controller = ResearchController()
        state = controller.build_state(tools_for(ResearchLedger()), turn=1, max_turns=8)

        plan = controller.parse_plan(
            JsonToolCodec(),
            '{"tool":"done","args":{"answer":"premature"}}',
            state,
        )

        self.assertEqual(plan.protocol_error_kind, "disallowed_tool")
        self.assertIn("not allowed", plan.protocol_error)

    def test_disallowed_tool_takes_precedence_over_bad_args(self) -> None:
        controller = ResearchController()
        state = controller.build_state(tools_for(ResearchLedger()), turn=1, max_turns=8)

        plan = controller.parse_plan(
            JsonToolCodec(),
            json.dumps({
                "tool": "knowledge_write",
                "args": {
                    "title": "Alpha report",
                    "content": "direct report",
                },
            }),
            state,
        )

        self.assertEqual(plan.protocol_error_kind, "disallowed_tool")
        self.assertIn("knowledge_write is not allowed", plan.protocol_error)
        self.assertNotIn("missing required arg", plan.protocol_error)

    def test_unknown_source_id_in_write_is_invalid_args(self) -> None:
        ledger = ResearchLedger()
        ledger.record_open("https://example.com/a", "https://example.com/a", "Alpha", "Alpha evidence text")
        controller = ResearchController()
        state = controller.build_state(tools_for(ledger), turn=3, max_turns=8)

        plan = controller.parse_plan(
            JsonToolCodec(),
            json.dumps({
                "tool": "knowledge_write",
                "args": {
                    "type": "fact",
                    "title": "Alpha",
                    "body": "Alpha evidence text",
                    "sources": ["s9"],
                },
            }),
            state,
        )

        self.assertEqual(plan.protocol_error_kind, "invalid_args")
        self.assertIn("unknown source_id: s9", plan.protocol_error)


if __name__ == "__main__":
    unittest.main()
