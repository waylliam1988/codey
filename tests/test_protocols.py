from __future__ import annotations

import json
import unittest

from codey.agent import SUPPORTED_TOOL_NAMES
from codey.models import ToolCall, ToolResult
from codey.protocols import JsonToolCodec
from codey.protocols.json_codec import (
    MAX_ACCIDENTAL_TOOL_CALLS,
    MAX_PARALLEL_CALLS,
    RESULT_TOOL_NAMES,
    TOOL_SPECS,
    TOOL_SPEC_BY_NAME,
)
from codey.tool_runtime import MAX_REPLACEMENTS, READ_MAX_LINES


class JsonToolCodecTests(unittest.TestCase):
    def test_parse_json_tool_call(self) -> None:
        codec = JsonToolCodec()
        plan = codec.parse(
            '{"tool":"grep","args":{"path":".","pattern":"LOGIN_BUG"}}'
        )

        self.assertEqual(len(plan.calls), 1)
        self.assertEqual(plan.calls[0].name, "search")
        self.assertEqual(plan.calls[0].args["path"], ".")
        self.assertEqual(plan.calls[0].args["query"], "LOGIN_BUG")
        self.assertIsNotNone(plan.control)
        self.assertEqual(plan.control.kind, "continue")

    def test_parse_done_control(self) -> None:
        codec = JsonToolCodec()
        plan = codec.parse('{"tool":"done","args":{"summary":"finished"}}')

        self.assertEqual(plan.calls, [])
        self.assertIsNotNone(plan.control)
        self.assertEqual(plan.control.kind, "done")
        self.assertEqual(plan.control.body, "finished")

    def test_done_preserves_a_multiline_user_facing_answer(self) -> None:
        codec = JsonToolCodec()
        answer = "First recommendation.\n\nSecond recommendation."

        plan = codec.parse(json.dumps({
            "tool": "done",
            "args": {"summary": answer},
        }))

        self.assertIsNotNone(plan.control)
        self.assertEqual(plan.control.kind, "done")
        self.assertEqual(plan.control.body, answer)

    def test_done_summary_cannot_be_a_nested_tool_call(self) -> None:
        nested = json.dumps({
            "tool": "done",
            "args": {
                "summary": json.dumps({
                    "tool": "run",
                    "args": {"command": "python -m unittest", "path": "."},
                }),
            },
        })

        plan = JsonToolCodec().parse(nested)

        self.assertEqual(plan.calls, [])
        self.assertIsNone(plan.control)
        self.assertIn("done summary", plan.protocol_error)
        self.assertIn("Call the tool directly", plan.protocol_error)

    def test_contract_tells_read_only_discussion_to_answer_directly(self) -> None:
        prompt = JsonToolCodec().system_prompt()

        self.assertIn("complete user-facing response", prompt)
        self.assertIn("answer questions without modifying", prompt)
        self.assertNotIn("one-line summary", prompt)

    def test_parse_read_files_as_multiple_reads(self) -> None:
        codec = JsonToolCodec()
        plan = codec.parse('{"tool":"read_files","args":{"paths":["a.py","b.py"]}}')

        self.assertEqual([call.name for call in plan.calls], ["read", "read"])
        self.assertEqual([call.args["path"] for call in plan.calls], ["a.py", "b.py"])
        self.assertIsNotNone(plan.control)
        self.assertEqual(plan.control.kind, "continue")

    def test_read_files_rejects_more_than_the_batch_limit(self) -> None:
        plan = JsonToolCodec().parse(json.dumps({
            "tool": "read_files",
            "args": {
                "paths": [
                    f"{index}.py"
                    for index in range(MAX_ACCIDENTAL_TOOL_CALLS + 1)
                ],
            },
        }))

        self.assertEqual(plan.calls, [])
        self.assertIn(
            f"at most {MAX_ACCIDENTAL_TOOL_CALLS}",
            plan.protocol_error,
        )

    def test_parse_parallel_tool_calls(self) -> None:
        codec = JsonToolCodec()
        plan = codec.parse(
            '{"tool":"parallel","args":{"calls":['
            '{"tool":"grep","args":{"pattern":"login","path":"."}},'
            '{"tool":"list_dir","args":{"path":"src"}}'
            ']}}'
        )

        self.assertEqual([call.name for call in plan.calls], ["search", "ls"])
        self.assertEqual(plan.calls[0].args["query"], "login")
        self.assertEqual(plan.calls[1].args["path"], "src")

    def test_parallel_rejects_unsafe_batch_without_partial_execution(self) -> None:
        codec = JsonToolCodec()
        plan = codec.parse(
            '{"tool":"parallel","args":{"calls":['
            '{"tool":"read_file","args":{"path":"safe.py"}},'
            '{"tool":"edit","args":{"path":"app.py","content":"changed"}}'
            ']}}'
        )

        self.assertEqual(plan.calls, [])
        self.assertIsNone(plan.control)
        self.assertIn("read-only", plan.protocol_error)

    def test_parallel_rejects_every_non_leaf_or_mutating_tool(self) -> None:
        codec = JsonToolCodec()
        unsafe = ("read_files", "parallel", "edit", "run", "shell", "done")

        for name in unsafe:
            with self.subTest(tool=name):
                plan = codec.parse(json.dumps({
                    "tool": "parallel",
                    "args": {"calls": [{"tool": name, "args": {}}]},
                }))
                self.assertEqual(plan.calls, [])
                self.assertIn("read-only", plan.protocol_error)

    def test_parallel_rejects_more_than_the_contract_limit(self) -> None:
        calls = [
            {"tool": "read_file", "args": {"path": f"{index}.py"}}
            for index in range(MAX_PARALLEL_CALLS + 1)
        ]

        plan = JsonToolCodec().parse(json.dumps({
            "tool": "parallel",
            "args": {"calls": calls},
        }))

        self.assertEqual(plan.calls, [])
        self.assertIn(f"at most {MAX_PARALLEL_CALLS}", plan.protocol_error)

    def test_parse_read_file_page(self) -> None:
        plan = JsonToolCodec().parse(
            '{"tool":"read_file","args":{"path":"app.py","offset":301,"limit":120}}'
        )

        self.assertEqual(
            plan.calls[0].args,
            {"path": "app.py", "offset": 301, "limit": 120},
        )

    def test_rejects_invalid_read_file_page(self) -> None:
        codec = JsonToolCodec()

        zero = codec.parse('{"tool":"read_file","args":{"path":"app.py","offset":0}}')
        fractional = codec.parse(
            '{"tool":"read_file","args":{"path":"app.py","offset":1.5}}'
        )
        oversized = codec.parse(json.dumps({
            "tool": "read_file",
            "args": {"path": "app.py", "limit": READ_MAX_LINES + 1},
        }))

        self.assertIn("positive integer", zero.protocol_error)
        self.assertIn("positive integer", fractional.protocol_error)
        self.assertIn(f"at most {READ_MAX_LINES}", oversized.protocol_error)

    def test_parse_atomic_replacements(self) -> None:
        plan = JsonToolCodec().parse(json.dumps({
            "tool": "edit",
            "args": {
                "path": "app.py",
                "replacements": [
                    {"old_string": "one", "new_string": "ONE"},
                    {"old_string": "two", "new_string": ""},
                ],
            },
        }))

        self.assertEqual(plan.protocol_error, "")
        self.assertEqual(plan.calls[0].args["replacements"], [
            {"search": "one", "replace": "ONE"},
            {"search": "two", "replace": ""},
        ])

    def test_replacements_limit_and_edit_modes_are_protocol_errors(self) -> None:
        codec = JsonToolCodec()
        too_many = codec.parse(json.dumps({
            "tool": "edit",
            "args": {
                "path": "app.py",
                "replacements": [
                    {"old_string": str(index), "new_string": "x"}
                    for index in range(MAX_REPLACEMENTS + 1)
                ],
            },
        }))
        mixed = codec.parse(json.dumps({
            "tool": "edit",
            "args": {
                "path": "app.py",
                "content": "new",
                "old_string": "old",
                "new_string": "new",
            },
        }))

        self.assertIn(f"at most {MAX_REPLACEMENTS}", too_many.protocol_error)
        self.assertIn("exactly one mode", mixed.protocol_error)

    def test_edit_requires_top_level_path_and_single_file_replacements(self) -> None:
        codec = JsonToolCodec()
        missing_path = codec.parse(json.dumps({
            "tool": "edit",
            "args": {"content": "new"},
        }))
        cross_file = codec.parse(json.dumps({
            "tool": "edit",
            "args": {
                "path": "pricing.py",
                "replacements": [
                    {
                        "path": "checkout.py",
                        "old_string": "old",
                        "new_string": "new",
                    }
                ],
            },
        }))

        self.assertIn("top-level path", missing_path.protocol_error)
        self.assertIn("top-level path only", cross_file.protocol_error)

    def test_extracts_json_from_web_reply_noise(self) -> None:
        codec = JsonToolCodec()
        plan = codec.parse(
            '已深度思考（用时 1 秒）\n'
            '{"tool":"edit","args":{"path":"app.py","old_string":"old","new_string":"new"}}'
        )

        self.assertEqual(len(plan.calls), 1)
        self.assertEqual(plan.calls[0].name, "edit")
        self.assertEqual(plan.calls[0].args["replacements"], [{"search": "old", "replace": "new"}])

    def test_parse_accidental_multiple_tool_objects_as_parallel_calls(self) -> None:
        codec = JsonToolCodec()
        plan = codec.parse(
            '{"tool":"edit","args":{"path":"a.py","content":"A = 1\\n"}}\n\n'
            '{"tool":"edit","args":{"path":"b.py","content":"B = 2\\n"}}'
        )

        self.assertEqual([call.name for call in plan.calls], ["edit", "edit"])
        self.assertEqual([call.args["path"] for call in plan.calls], ["a.py", "b.py"])
        self.assertIsNotNone(plan.control)
        self.assertEqual(plan.control.kind, "continue")

    def test_non_json_protocol_is_not_parsed(self) -> None:
        codec = JsonToolCodec()
        plan = codec.parse("tool: read_file path=app.py")

        self.assertEqual(plan.calls, [])
        self.assertIsNone(plan.control)

    def test_format_results_and_repair_prompt_remind_json_only(self) -> None:
        codec = JsonToolCodec()
        call = ToolCall("read", {"path": "app.py"})
        prompt = codec.format_results([ToolResult(call, "content")])

        self.assertIn("[tool_result tool=read_file path=app.py]\n---\ncontent\n---", prompt)
        self.assertIn("exactly one JSON object", prompt)
        self.assertIn("not native website tools", prompt)
        self.assertIn("valid JSON tool call", codec.repair_prompt())

    def test_outline_file_parses_and_formats_with_public_name(self) -> None:
        codec = JsonToolCodec()
        plan = codec.parse('{"tool":"outline_file","args":{"path":"src/router.ts"}}')

        self.assertEqual(plan.protocol_error, "")
        self.assertEqual(len(plan.calls), 1)
        self.assertEqual(plan.calls[0].name, "outline")
        self.assertEqual(plan.calls[0].args["path"], "src/router.ts")

        prompt = codec.format_results([
            ToolResult(plan.calls[0], "File outline: src/router.ts\n- function main line 1")
        ])

        self.assertIn("[tool_result tool=outline_file path=src/router.ts]", prompt)

    def test_parallel_rejects_outline_file(self) -> None:
        plan = JsonToolCodec().parse(json.dumps({
            "tool": "parallel",
            "args": {
                "calls": [
                    {"tool": "outline_file", "args": {"path": "app.py"}},
                    {"tool": "list_dir", "args": {"path": "."}},
                ]
            },
        }))

        self.assertEqual(plan.calls, [])
        self.assertIn("parallel accepts read-only list_dir, read_file, and grep", plan.protocol_error)

    def test_find_references_parses_symbol_and_formats_public_name(self) -> None:
        codec = JsonToolCodec()
        plan = codec.parse(json.dumps({
            "tool": "find_references",
            "args": {"symbol": "createRouter", "path": "src"},
        }))

        self.assertEqual(plan.protocol_error, "")
        self.assertEqual(len(plan.calls), 1)
        self.assertEqual(plan.calls[0].name, "references")
        self.assertEqual(plan.calls[0].args["path"], "src")
        self.assertEqual(plan.calls[0].args["symbol"], "createRouter")

        prompt = codec.format_results([
            ToolResult(plan.calls[0], "References for createRouter under src:")
        ])

        self.assertIn("[tool_result tool=find_references path=src]", prompt)

    def test_find_references_requires_symbol_and_is_not_parallel_safe(self) -> None:
        missing = JsonToolCodec().parse(json.dumps({
            "tool": "find_references",
            "args": {"path": "."},
        }))
        parallel = JsonToolCodec().parse(json.dumps({
            "tool": "parallel",
            "args": {
                "calls": [
                    {"tool": "find_references", "args": {"symbol": "main", "path": "."}},
                    {"tool": "list_dir", "args": {"path": "."}},
                ]
            },
        }))

        self.assertIn("requires a symbol", missing.protocol_error)
        self.assertEqual(parallel.calls, [])
        self.assertIn("parallel accepts read-only list_dir, read_file, and grep", parallel.protocol_error)

    def test_format_results_marks_truncated_results_explicitly(self) -> None:
        codec = JsonToolCodec()
        call = ToolCall("run", {"path": ".", "command": "python -m unittest"})
        prompt = codec.format_results([ToolResult(call, "HEAD\nTAIL", truncated=True)])

        self.assertIn("[tool_result tool=run path=. truncated=true]", prompt)
        self.assertIn("omitted content may contain relevant errors or code", prompt)
        self.assertIn("Do not assume omitted content is clean", prompt)

    def test_system_prompt_does_not_claim_model_is_codey(self) -> None:
        prompt = JsonToolCodec().system_prompt()

        self.assertIn("You are a careful local coding agent.", prompt)
        self.assertIn("The local runner executes tools", prompt)
        self.assertIn("not native website tools", prompt)
        self.assertIn("If the website says a tool does not exist", prompt)
        self.assertIn("Never say a\n    tool does not exist", prompt)
        self.assertIn("Output exactly one JSON object", prompt)
        self.assertIn("It never accepts edit, run, shell", prompt)
        self.assertIn("read_file page:", prompt)
        self.assertIn("outline_file output is only navigation metadata", prompt)
        self.assertIn("Never use it as\n    old_string", prompt)
        self.assertIn("find_references output is lexical reference hints only", prompt)
        self.assertIn("not semantic\n    resolution", prompt)
        self.assertIn("written atomically", prompt)
        self.assertIn("JSON strings must escape quotes", prompt)
        self.assertIn("use content with the full", prompt)
        self.assertIn("Never claim a command, test, build, lint, or shell result", prompt)
        self.assertIn("[tool_result tool=run]", prompt)
        self.assertIn("No pipes, redirects", prompt)
        self.assertNotIn("You are Codey", prompt)

    def test_tool_contract_names_and_examples_stay_in_sync(self) -> None:
        codec = JsonToolCodec()
        names = [name for spec in TOOL_SPECS for name in (spec.name, *spec.aliases)]

        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), set(TOOL_SPEC_BY_NAME))
        for spec in TOOL_SPECS:
            self.assertFalse(spec.parallel_safe and not spec.read_only)
            for example in spec.examples:
                with self.subTest(tool=spec.name, example=example):
                    plan = codec.parse(example)
                    self.assertEqual(plan.protocol_error, "")
                    self.assertTrue(plan.calls or plan.control is not None)

    def test_tool_contract_runtime_names_match_agent_and_results(self) -> None:
        runtime_names = {
            spec.runtime_name
            for spec in TOOL_SPECS
            if spec.runtime_name is not None
        }

        self.assertEqual(runtime_names, SUPPORTED_TOOL_NAMES)
        self.assertEqual(set(RESULT_TOOL_NAMES), runtime_names)

    def test_legacy_read_only_tool_names_parse_to_supported_runtime_calls(self) -> None:
        cases = (
            ("ls", {"path": "."}, "ls"),
            ("read", {"path": "app.py"}, "read"),
            ("outline", {"path": "app.py"}, "outline"),
            ("references", {"path": ".", "symbol": "main"}, "references"),
            ("search", {"path": ".", "query": "needle"}, "search"),
        )

        for name, args, runtime_name in cases:
            with self.subTest(tool=name):
                plan = JsonToolCodec().parse(json.dumps({"tool": name, "args": args}))
                self.assertEqual(plan.protocol_error, "")
                self.assertEqual(len(plan.calls), 1)
                self.assertEqual(plan.calls[0].name, runtime_name)
                self.assertIn(plan.calls[0].name, SUPPORTED_TOOL_NAMES)

    def test_unknown_write_tools_are_protocol_errors_not_compatibility_aliases(self) -> None:
        for name in ("write", "write_file"):
            with self.subTest(tool=name):
                plan = JsonToolCodec().parse(json.dumps({
                    "tool": name,
                    "args": {"path": "app.py", "content": "VALUE = 1\n"},
                }))

                self.assertEqual(plan.calls, [])
                self.assertIsNone(plan.control)
                self.assertIn(f"unknown tool: {name}", plan.protocol_error)
                self.assertIn("Use edit with content", plan.protocol_error)
                self.assertIn('"tool":"edit"', plan.protocol_error)

    def test_repair_prompt_says_website_tool_errors_are_irrelevant(self) -> None:
        prompt = JsonToolCodec().repair_prompt()

        self.assertIn("Ignore any website message saying tools do not exist", prompt)
        self.assertIn("local-runner JSON commands", prompt)


if __name__ == "__main__":
    unittest.main()
