from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from codey.runtime.models import ToolCall, ToolResult
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
from codey.research.runner import render_research_repair_prompt
from codey.research.source_document import SourceDocument, SourcePage
from codey.research.tool_contract import PROTOCOL_NO_JSON


def tools_for(ledger: ResearchLedger) -> SimpleNamespace:
    return SimpleNamespace(
        ledger=ledger,
        created_ids=[],
        updated_ids=[],
        grounded_ids=set(),
    )


def ledger_with_evidence() -> ResearchLedger:
    ledger = ResearchLedger()
    ledger.record_search("alpha", [{"title": "Alpha", "url": "https://example.com/a"}])
    ledger.record_open("https://example.com/a", "https://example.com/a", "Alpha", "Alpha evidence text")
    ledger.record_source_search("https://example.com/a", "Alpha", [{"offset": 0, "snippet": "Alpha evidence"}])
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
    return ledger


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

    def test_priority_connector_results_must_be_opened_before_ordinary_results(self) -> None:
        ledger = ResearchLedger()
        ledger.record_search(
            "immune checkpoint inhibitor hepatotoxicity",
            [
                {
                    "title": "PubMed: ICI hepatotoxicity",
                    "url": "https://pubmed.ncbi.nlm.nih.gov/41142624/",
                    "snippet": "medical abstract",
                },
                {
                    "title": "General web result",
                    "url": "https://example.com/general",
                    "snippet": "general page",
                },
            ],
        )
        controller = ResearchController()
        state = controller.build_state(tools_for(ledger), turn=2, max_turns=8)

        self.assertEqual(state.allowed_tools, ("open_result",))
        self.assertEqual(state.priority_result_ids, ("r1",))
        self.assertEqual(len(state.result_lines), 1)
        self.assertIn("pubmed.ncbi.nlm.nih.gov", state.result_lines[0])
        self.assertNotIn("example.com/general", "\n".join(state.result_lines))
        self.assertIn("Priority source results", render_control_block(state))
        plan = controller.parse_plan(
            JsonToolCodec(),
            '{"tool":"open_result","args":{"result_id":"r2"}}',
            state,
        )

        self.assertEqual(plan.protocol_error_kind, "invalid_args")
        self.assertIn("priority source result_id", plan.protocol_error)
        self.assertFalse(plan.calls)

    def test_priority_connector_example_uses_priority_id_when_not_first_result(self) -> None:
        ledger = ResearchLedger()
        ledger.record_search(
            "immune checkpoint inhibitor hepatotoxicity",
            [
                {
                    "title": "General web result",
                    "url": "https://example.com/general",
                    "snippet": "general page",
                },
                {
                    "title": "PubMed: ICI hepatotoxicity",
                    "url": "https://pubmed.ncbi.nlm.nih.gov/41142624/",
                    "snippet": "medical abstract",
                },
            ],
        )
        controller = ResearchController()
        state = controller.build_state(tools_for(ledger), turn=2, max_turns=8)
        block = render_control_block(state)

        self.assertEqual(state.priority_result_ids, ("r2",))
        self.assertIn('{"tool":"open_result","args":{"result_id":"r2"}}', block)
        self.assertNotIn('{"tool":"open_result","args":{"result_id":"r1"}}', block)

    def test_failed_priority_connector_results_stop_blocking_normal_flow(self) -> None:
        first_pubmed = "https://pubmed.ncbi.nlm.nih.gov/41142624/"
        second_pubmed = "https://pubmed.ncbi.nlm.nih.gov/41972337/"
        general = "https://example.com/general"
        ledger = ResearchLedger()
        ledger.record_search(
            "immune checkpoint inhibitor hepatotoxicity",
            [
                {"title": "PubMed first", "url": first_pubmed, "snippet": "medical abstract"},
                {"title": "PubMed second", "url": second_pubmed, "snippet": "medical abstract"},
                {"title": "General web result", "url": general, "snippet": "general page"},
            ],
        )
        controller = ResearchController()
        first_state = controller.build_state(tools_for(ledger), turn=2, max_turns=8)

        controller.record_tool_outcome(
            first_state,
            ToolCall("open_url", {"url": first_pubmed}),
            "ERROR: open failed",
        )
        second_state = controller.build_state(tools_for(ledger), turn=3, max_turns=8)

        self.assertEqual(second_state.allowed_tools, ("open_result",))
        self.assertEqual(second_state.priority_result_ids, ("r2",))
        self.assertNotIn("r1", second_state.result_urls)
        self.assertIn("r3", second_state.result_urls)
        self.assertIn("r2:", "\n".join(second_state.result_lines))
        self.assertNotIn("r1:", "\n".join(second_state.result_lines))

        controller.record_tool_outcome(
            second_state,
            ToolCall("open_url", {"url": second_pubmed}),
            "SKIPPED: unsupported content",
        )
        third_state = controller.build_state(tools_for(ledger), turn=4, max_turns=8)
        plan = controller.parse_plan(
            JsonToolCodec(),
            '{"tool":"open_result","args":{"result_id":"r3"}}',
            third_state,
        )

        self.assertEqual(third_state.priority_result_ids, ())
        self.assertIn("web_search", third_state.allowed_tools)
        self.assertIn("open_result", third_state.allowed_tools)
        self.assertEqual(third_state.result_urls, {"r3": general})
        self.assertIn("Search results you may open", render_control_block(third_state))
        self.assertFalse(plan.protocol_error)
        self.assertEqual(plan.calls[0].args["url"], general)

    def test_priority_connector_result_limit_clears_after_connector_source_open(self) -> None:
        ledger = ResearchLedger()
        ledger.record_search(
            "immune checkpoint inhibitor hepatotoxicity",
            [
                {
                    "title": "PubMed: ICI hepatotoxicity",
                    "url": "https://pubmed.ncbi.nlm.nih.gov/41142624/",
                    "snippet": "medical abstract",
                },
                {
                    "title": "General web result",
                    "url": "https://example.com/general",
                    "snippet": "general page",
                },
            ],
        )
        ledger.record_open(
            "https://pubmed.ncbi.nlm.nih.gov/41142624/",
            "https://pubmed.ncbi.nlm.nih.gov/41142624/",
            "PubMed",
            "medical abstract text",
        )
        controller = ResearchController()
        state = controller.build_state(tools_for(ledger), turn=3, max_turns=8)

        self.assertEqual(state.priority_result_ids, ())
        plan = controller.parse_plan(
            JsonToolCodec(),
            '{"tool":"open_result","args":{"result_id":"r2"}}',
            state,
        )

        self.assertFalse(plan.protocol_error)
        self.assertEqual(plan.calls[0].args["url"], "https://example.com/general")

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

    def test_controller_rejects_name_field_as_hidden_tool_alias(self) -> None:
        controller = ResearchController()
        state = controller.build_state(tools_for(ResearchLedger()), turn=1, max_turns=8)

        plan = controller.parse_plan(
            JsonToolCodec(),
            '{"name":"web_search","args":{"query":"alpha"}}',
            state,
        )

        self.assertEqual(plan.protocol_error_kind, "invalid_args")
        self.assertIn('exactly top-level "tool" and "args"', plan.protocol_error)
        self.assertFalse(plan.calls)

    def test_controller_rejects_top_level_args_shape(self) -> None:
        controller = ResearchController()
        state = controller.build_state(tools_for(ResearchLedger()), turn=1, max_turns=8)

        plan = controller.parse_plan(
            JsonToolCodec(),
            '{"tool":"web_search","query":"alpha"}',
            state,
        )

        self.assertEqual(plan.protocol_error_kind, "invalid_args")
        self.assertIn('exactly top-level "tool" and "args"', plan.protocol_error)
        self.assertFalse(plan.calls)

    def test_controller_rejects_extra_top_level_fields(self) -> None:
        controller = ResearchController()
        state = controller.build_state(tools_for(ResearchLedger()), turn=1, max_turns=8)

        plan = controller.parse_plan(
            JsonToolCodec(),
            '{"tool":"web_search","args":{"query":"alpha"},"query":"SECRET"}',
            state,
        )

        self.assertEqual(plan.protocol_error_kind, "invalid_args")
        self.assertIn('exactly top-level "tool" and "args"', plan.protocol_error)
        self.assertFalse(plan.calls)

    def test_controller_rejects_extra_json_object_before_valid_tool_call(self) -> None:
        controller = ResearchController()
        state = controller.build_state(tools_for(ResearchLedger()), turn=1, max_turns=8)

        plan = controller.parse_plan(
            JsonToolCodec(),
            '{"foo":"bar"}\n{"tool":"web_search","args":{"query":"alpha"}}',
            state,
        )

        self.assertEqual(plan.protocol_error_kind, "too_many_tools")
        self.assertIn("too many JSON tool calls", plan.protocol_error)
        self.assertFalse(plan.calls)

    def test_controller_rejects_non_plain_json_object_wrappers(self) -> None:
        controller = ResearchController()
        state = controller.build_state(tools_for(ResearchLedger()), turn=1, max_turns=8)
        payload = '{"tool":"web_search","args":{"query":"alpha"}}'

        for reply in (
            f"[{payload}]",
            f"```json\n{payload}\n```",
            f"Here is the call: {payload}",
        ):
            plan = controller.parse_plan(JsonToolCodec(), reply, state)

            self.assertEqual(plan.protocol_error_kind, "invalid_args")
            self.assertIn("exactly one JSON object and nothing else", plan.protocol_error)
            self.assertFalse(plan.calls)

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
        ledger = ledger_with_evidence()
        with_evidence = controller.build_state(tools_for(ledger), turn=3, max_turns=8)

        self.assertNotIn("done", early.allowed_tools)
        self.assertIn("done", late.allowed_tools)
        self.assertTrue(late.done_escape)
        self.assertIn("done", with_evidence.allowed_tools)
        self.assertFalse(with_evidence.finish_required)

    def test_near_limit_with_evidence_switches_to_finish_actions(self) -> None:
        controller = ResearchController()
        ledger = ledger_with_evidence()
        tools = tools_for(ledger)
        tools.created_ids = ["fact-1", "fact-2"]

        state = controller.build_state(tools, turn=6, max_turns=8)

        self.assertTrue(state.finish_required)
        self.assertEqual(state.allowed_tools, ("done", "knowledge_write", "knowledge_link"))
        self.assertNotIn("web_search", state.allowed_tools)
        self.assertNotIn("open_result", state.allowed_tools)
        self.assertNotIn("reopen_source", state.allowed_tools)
        self.assertNotIn("open_hit", state.allowed_tools)
        self.assertNotIn("source_search", state.allowed_tools)
        self.assertIn("Finish now:", render_control_block(state))

    def test_finish_repair_prefers_done_example_after_no_json_reply(self) -> None:
        controller = ResearchController()
        state = controller.build_state(tools_for(ledger_with_evidence()), turn=6, max_turns=8)

        plan = controller.parse_plan(JsonToolCodec(), "I can now answer the question.", state)
        prompt = render_research_repair_prompt(JsonToolCodec(), plan, state)

        self.assertEqual(plan.protocol_error_kind, PROTOCOL_NO_JSON)
        self.assertIn('{"tool":"done"', prompt)
        self.assertNotIn('{"tool":"knowledge_search"', prompt)

    def test_finish_state_accepts_plain_final_report_as_done(self) -> None:
        controller = ResearchController()
        state = controller.build_state(tools_for(ledger_with_evidence()), turn=6, max_turns=8)
        report = (
            "结论\n"
            "Alpha has evidence [1].\n\n"
            "关键证据\n"
            "- Alpha evidence text [1].\n\n"
            "来源\n"
            "[1] https://example.com/a\n"
        )

        plan = controller.parse_plan(JsonToolCodec(), report, state)

        self.assertFalse(plan.protocol_error)
        self.assertIsNotNone(plan.control)
        self.assertEqual(plan.control.kind, "done")
        self.assertEqual(plan.control.body, report.strip())

    def test_finish_state_does_not_accept_short_prose_as_done(self) -> None:
        controller = ResearchController()
        state = controller.build_state(tools_for(ledger_with_evidence()), turn=6, max_turns=8)

        plan = controller.parse_plan(JsonToolCodec(), "I can now answer the question.", state)

        self.assertEqual(plan.protocol_error_kind, PROTOCOL_NO_JSON)
        self.assertIsNone(plan.control)

    def test_plain_final_report_is_not_recovered_before_finish_state(self) -> None:
        controller = ResearchController()
        state = controller.build_state(tools_for(ledger_with_evidence()), turn=3, max_turns=8)
        report = (
            "结论\n"
            "Alpha has evidence [1].\n\n"
            "关键证据\n"
            "- Alpha evidence text [1].\n\n"
            "来源\n"
            "[1] https://example.com/a\n"
        )

        plan = controller.parse_plan(JsonToolCodec(), report, state)

        self.assertIsNone(plan.control)
        self.assertTrue(plan.protocol_error)

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
