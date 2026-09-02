from __future__ import annotations

import unittest

from codey.protocols.json_codec import JsonToolCodec
from codey.research.tool_contract import TOOL_CONTRACTS as RESEARCH_CONTRACTS
from codey.tool_args_repair import (
    ARG_REPAIR_POLICY,
    COMMAND_KEYS,
    EDIT_NEW_KEYS,
    EDIT_OLD_KEYS,
    PATH_ARG_KEYS,
    REFERENCES_SYMBOL_KEYS,
    SEARCH_QUERY_KEYS,
)
from codey.tool_prompt import render_coding_tool_contract_text
from codey.toolchain.definition import TOOL_DEFINITIONS


class ToolContractDriftTests(unittest.TestCase):
    def test_alias_constants_cover_all_normalizer_groups(self) -> None:
        # every alias tuple used by the normalizer must be the exported constant
        self.assertEqual(PATH_ARG_KEYS, ("path", "cwd"))
        self.assertEqual(SEARCH_QUERY_KEYS, ("query", "pattern"))
        self.assertEqual(REFERENCES_SYMBOL_KEYS, ("symbol", "name"))
        self.assertEqual(COMMAND_KEYS, ("command", "cmd"))
        self.assertEqual(EDIT_OLD_KEYS, ("old_string", "search", "old", "before"))
        self.assertEqual(EDIT_NEW_KEYS, ("new_string", "replace", "replacement", "after", "new"))

    def test_arg_repair_policy_contains_all_recorded_kinds(self) -> None:
        # parser telemetry may only emit kinds that are declared in the policy
        expected_kinds = {
            "path_alias",
            "path_normalized",
            "search_field_alias",
            "references_field_alias",
            "command_field_alias",
            "edit_field_alias",
            "numeric_coerced",
            "json_replacements_parsed",
            "replacement_object_wrapped",
        }
        self.assertEqual(set(ARG_REPAIR_POLICY.keys()), expected_kinds)

    def test_parser_aliases_are_policy_declared(self) -> None:
        codec = JsonToolCodec()
        # emit one of each alias shape and ensure the recorded kind is in policy
        cases = [
            ('{"tool":"grep","args":{"pattern":"x","path":"."}}', "search_field_alias"),
            ('{"tool":"read_file","args":{"path":"app.py","offset":"5"}}', "numeric_coerced"),
            ('{"tool":"edit","args":{"path":"app.py","old":"old","new":"new"}}', "edit_field_alias"),
        ]
        for payload, expected_kind in cases:
            with self.subTest(payload=payload):
                plan = codec.parse(payload)
                self.assertEqual(plan.protocol_error, "")
                self.assertIn(expected_kind, plan.arg_repair_counts)
                self.assertIn(expected_kind, ARG_REPAIR_POLICY)

    def test_runtime_only_fields_do_not_enter_model_contract(self) -> None:
        # model-visible contract text must not contain runtime-only vocabulary
        text = render_coding_tool_contract_text()
        # runtime_name values like "ls" for list_dir are not in the visible examples
        # list_dir example uses "list_dir", not "ls"
        self.assertIn('"tool":"list_dir"', text)
        self.assertNotIn('"tool":"ls"', text)
        # find_references example uses "find_references", not runtime "references"
        self.assertIn('"tool":"find_references"', text)
        # grep example uses "grep", not "search"
        self.assertIn('"tool":"grep"', text)

    def test_research_tools_are_not_renamed_to_coding_names(self) -> None:
        # research must keep open_url and knowledge_write, not read/write
        self.assertIn("open_url", RESEARCH_CONTRACTS)
        self.assertIn("knowledge_write", RESEARCH_CONTRACTS)
        self.assertNotIn("read", RESEARCH_CONTRACTS)
        self.assertNotIn("write", RESEARCH_CONTRACTS)
        # coding contract must not leak research tools
        coding_names = {d.name for d in TOOL_DEFINITIONS}
        self.assertNotIn("open_url", coding_names)
        self.assertNotIn("knowledge_write", coding_names)


if __name__ == "__main__":
    unittest.main()
