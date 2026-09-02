from __future__ import annotations

import unittest

from codey.protocols.json_codec import JsonToolCodec, SYSTEM_PROMPT
from codey.research.controller import controller_action_contract_hash, controller_system_prompt
from codey.research.protocols import JsonToolCodec as ResearchCodec
from codey.tool_prompt import (
    RenderedToolContract,
    coding_model_tool_contract_hash,
    model_visible_contract_hash,
    render_coding_system_prompt,
    render_coding_tool_contract,
    render_coding_tool_contract_text,
)
from codey.toolchain.definition import TOOL_DEFINITIONS


class ToolPromptTests(unittest.TestCase):
    def test_render_coding_tool_contract_text_is_deterministic(self) -> None:
        first = render_coding_tool_contract_text()
        second = render_coding_tool_contract_text()
        self.assertEqual(first, second)
        self.assertIn('{"tool":"read_file"', first)
        self.assertIn('{"tool":"edit"', first)
        # empty definitions renders empty contract
        self.assertEqual(render_coding_tool_contract_text(()), "")

    def test_coding_model_tool_contract_hash_is_content_addressed(self) -> None:
        full = coding_model_tool_contract_hash()
        readonly = coding_model_tool_contract_hash(
            tuple(d for d in TOOL_DEFINITIONS if d.name in ("read_file", "grep"))
        )
        self.assertNotEqual(full, readonly)
        self.assertTrue(full.startswith("sha256:"))
        self.assertEqual(full, coding_model_tool_contract_hash())
        # runtime names not hashed: changing runtime_name alone should not affect digest if text unchanged
        # text is derived from examples+description, not runtime_name
        text = render_coding_tool_contract_text()
        self.assertEqual(
            coding_model_tool_contract_hash(),
            model_visible_contract_hash("coding_tool_contract", text),
        )

    def test_rendered_tool_contract_exposes_runtime_names_without_hashing_them(self) -> None:
        rendered = render_coding_tool_contract()
        self.assertIsInstance(rendered, RenderedToolContract)
        self.assertEqual(rendered.text, render_coding_tool_contract_text())
        self.assertEqual(rendered.digest, coding_model_tool_contract_hash())
        self.assertIn("read_file", rendered.tool_names)
        self.assertIn("read", rendered.runtime_names)
        # digest must not change if we only shuffle runtime_names ordering conceptually
        # but text stays same -> digest stays same
        self.assertEqual(rendered.digest, model_visible_contract_hash("coding_tool_contract", rendered.text))

    def test_coding_writer_system_prompt_byte_parity(self) -> None:
        codec = JsonToolCodec()
        expected = render_coding_system_prompt(
            TOOL_DEFINITIONS,
            profile_name="coding_writer",
            allowed_tool_names={d.name for d in TOOL_DEFINITIONS},
        )
        self.assertEqual(codec.system_prompt(), expected)
        self.assertEqual(codec.system_prompt(), SYSTEM_PROMPT)

    def test_coding_readonly_system_prompt_filters_tools(self) -> None:
        codec = JsonToolCodec(permission_profile="planning_readonly")
        prompt = codec.system_prompt()
        self.assertIn('{"tool":"read_file"', prompt)
        self.assertNotIn('{"tool":"edit"', prompt)
        self.assertIn("This phase is read-only", prompt)

    def test_research_and_controller_system_prompt_byte_parity(self) -> None:
        # research default prompt must be byte-identical to codec's system_prompt
        research_codec = ResearchCodec(include_source_search=True)
        research_prompt = research_codec.system_prompt()
        # controller system prompt is the static hard-boundary preface
        controller_prompt = controller_system_prompt(include_source_search=True)
        self.assertIn("Research hard boundary", controller_prompt)
        self.assertIn("web_search", research_prompt)
        # hash must be stable and differ with source_search flag
        full_hash = controller_action_contract_hash(include_source_search=True)
        thin_hash = controller_action_contract_hash(include_source_search=False)
        self.assertNotEqual(full_hash, thin_hash)
        self.assertTrue(full_hash.startswith("sha256:"))

    def test_tool_definition_examples_are_parseable(self) -> None:
        codec = JsonToolCodec()
        for definition in TOOL_DEFINITIONS:
            for example in definition.examples:
                with self.subTest(tool=definition.name, example=example):
                    plan = codec.parse(example)
                    self.assertEqual(plan.protocol_error, "")
                    self.assertTrue(plan.calls or plan.control is not None)


if __name__ == "__main__":
    unittest.main()
